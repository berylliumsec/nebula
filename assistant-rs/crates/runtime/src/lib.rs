//! Bounded assistant scheduling policy. No tools, providers or agents are executed.
//!
//! The owning service must durably admit/claim work before acknowledging/dispatching
//! it. This deterministic policy is deliberately separate from persistence and I/O.

use nebula_assistant_domain::{ValidationError, bounded};
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, VecDeque},
    sync::Arc,
};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Work {
    pub id: String,
    pub project_id: String,
    /// Root parent conversation, or own session for a top-level turn.
    pub parent_group: String,
    pub session_id: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    /// Capacity is held, but durable admission has not been confirmed.
    Reserved,
    Ready,
    Running,
    Waiting,
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    InvalidWork(#[from] ValidationError),
    #[error("assistant admission capacity is full")]
    Capacity,
    #[error("work identifier already exists with different content")]
    Conflict,
    #[error("work does not exist or its state does not permit this transition")]
    InvalidTransition,
    #[error("running and pending capacities must be between 1 and 65536")]
    InvalidConfiguration,
    #[error("assistant reservation generations are exhausted")]
    GenerationExhausted,
}

/// A queue-bound reservation generation. Dropping a ticket never cancels work:
/// the writer may already have accepted it. Commit or abort after learning the
/// durable result. Clones permit duplicate delivery to be rejected safely.
#[derive(Clone)]
#[must_use = "a reservation remains allocated until explicitly committed or aborted"]
pub struct Reservation {
    id: String,
    generation: u64,
    queue: Arc<()>,
}
impl Reservation {
    pub fn work_id(&self) -> &str {
        &self.id
    }
}
impl std::fmt::Debug for Reservation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Reservation")
            .field("work_id", &self.id)
            .finish_non_exhaustive()
    }
}

#[derive(Clone)]
struct Entry {
    work: Work,
    state: State,
    generation: u64,
}
struct Group {
    id: String,
    ready: VecDeque<String>,
}
struct Project {
    id: String,
    groups: VecDeque<Group>,
}

pub struct FairQueue {
    running_limit: usize,
    pending_limit: usize,
    entries: HashMap<String, Entry>,
    projects: VecDeque<Project>,
    session_owners: HashMap<String, String>,
    // Every unresolved admission, including Reserved, participates in Session FIFO.
    session_order: HashMap<String, VecDeque<String>>,
    identity: Arc<()>,
    next_generation: Option<u64>,
    running: usize,
}

impl Default for FairQueue {
    fn default() -> Self {
        Self::new(128, 2048).expect("constant limits are valid")
    }
}

impl FairQueue {
    pub fn new(running_limit: usize, pending_limit: usize) -> Result<Self, Error> {
        if !(1..=65536).contains(&running_limit) || !(1..=65536).contains(&pending_limit) {
            return Err(Error::InvalidConfiguration);
        }
        Ok(Self {
            running_limit,
            pending_limit,
            entries: HashMap::new(),
            projects: VecDeque::new(),
            session_owners: HashMap::new(),
            session_order: HashMap::new(),
            identity: Arc::new(()),
            next_generation: Some(1),
            running: 0,
        })
    }

    /// Reserve capacity without making work dispatchable. Identical existing work
    /// is an invalid transition; callers cannot obtain someone else's ticket.
    pub fn reserve(&mut self, work: Work) -> Result<Reservation, Error> {
        Self::validate_work(&work)?;
        if let Some(existing) = self.entries.get(&work.id) {
            return Err(if existing.work == work {
                Error::InvalidTransition
            } else {
                Error::Conflict
            });
        }
        // Waiting work consumes admission capacity. Already-running work can
        // always park; that reserved capacity is bounded by running_limit.
        if self.entries.len() - self.running >= self.pending_limit
            || self.entries.len() >= self.pending_limit + self.running_limit
        {
            return Err(Error::Capacity);
        }
        let generation = self.next_generation.ok_or(Error::GenerationExhausted)?;
        let ticket = Reservation {
            id: work.id.clone(),
            generation,
            queue: self.identity.clone(),
        };
        self.session_order
            .entry(work.session_id.clone())
            .or_default()
            .push_back(work.id.clone());
        self.entries.insert(
            work.id.clone(),
            Entry {
                work,
                state: State::Reserved,
                generation,
            },
        );
        // The final representable generation is valid; subsequent reservations
        // fail without modifying any index. Never wrap or reuse a generation.
        self.next_generation = generation.checked_add(1);
        Ok(ticket)
    }

    /// Mark this exact reservation Ready after the owning service confirms its
    /// admission commit. Duplicate/stale/cross-queue tickets cannot enqueue it.
    pub fn commit(&mut self, reservation: Reservation) -> Result<(), Error> {
        self.validate_reservation(&reservation)?;
        let entry = self
            .entries
            .get_mut(&reservation.id)
            .expect("validated reservation");
        entry.state = State::Ready;
        let work = entry.work.clone();
        self.enqueue(&work);
        Ok(())
    }

    /// Release only this uncommitted reservation after confirmed admission
    /// failure. An unknown writer outcome must remain reserved for reconciliation.
    pub fn abort(&mut self, reservation: Reservation) -> Result<Work, Error> {
        self.validate_reservation(&reservation)?;
        self.remove_entry(&reservation.id)
    }

    /// Convenience for already durably admitted work. Returns false for an
    /// identical committed admission. A Reserved entry still requires its ticket
    /// and cannot be made dispatchable through this compatibility API.
    pub fn admit(&mut self, work: Work) -> Result<bool, Error> {
        Self::validate_work(&work)?;
        if let Some(existing) = self.entries.get(&work.id) {
            return if existing.work != work {
                Err(Error::Conflict)
            } else if existing.state == State::Reserved {
                Err(Error::InvalidTransition)
            } else {
                Ok(false)
            };
        }
        let reservation = self.reserve(work)?;
        self.commit(reservation)?;
        Ok(true)
    }

    fn validate_work(work: &Work) -> Result<(), Error> {
        for value in [
            &work.id,
            &work.project_id,
            &work.parent_group,
            &work.session_id,
        ] {
            bounded(value, 200)?;
        }
        Ok(())
    }
    fn validate_reservation(&self, ticket: &Reservation) -> Result<(), Error> {
        if !Arc::ptr_eq(&self.identity, &ticket.queue)
            || !self.entries.get(&ticket.id).is_some_and(|entry| {
                entry.state == State::Reserved && entry.generation == ticket.generation
            })
        {
            return Err(Error::InvalidTransition);
        }
        Ok(())
    }

    /// Round robin across projects, then root-parent groups. Within a session,
    /// ready work remains FIFO and a waiting turn retains session ownership.
    pub fn start_next(&mut self) -> Option<Work> {
        if self.running >= self.running_limit {
            return None;
        }
        for _ in 0..self.projects.len() {
            let mut project = self.projects.pop_front()?;
            let mut chosen = None;
            for _ in 0..project.groups.len() {
                let mut group = project.groups.pop_front()?;
                let index = group.ready.iter().position(|id| {
                    self.entries.get(id).is_some_and(|entry| {
                        entry.state == State::Ready
                            && self
                                .session_order
                                .get(&entry.work.session_id)
                                .and_then(|order| order.front())
                                .is_some_and(|first| first == id)
                            && self
                                .session_owners
                                .get(&entry.work.session_id)
                                .is_none_or(|owner| owner == id)
                    })
                });
                if let Some(index) = index {
                    let id = group.ready.remove(index)?;
                    chosen = self.entries.get(&id).map(|entry| entry.work.clone());
                }
                if !group.ready.is_empty() {
                    project.groups.push_back(group);
                }
                if chosen.is_some() {
                    break;
                }
            }
            if !project.groups.is_empty() {
                self.projects.push_back(project);
            }
            if let Some(work) = chosen {
                self.entries.get_mut(&work.id)?.state = State::Running;
                self.session_owners
                    .insert(work.session_id.clone(), work.id.clone());
                self.running += 1;
                return Some(work);
            }
        }
        None
    }

    /// Parent awaits child/approval/callback without holding an execution slot.
    pub fn park(&mut self, id: &str) -> Result<(), Error> {
        let entry = self.entries.get_mut(id).ok_or(Error::InvalidTransition)?;
        if entry.state != State::Running {
            return Err(Error::InvalidTransition);
        }
        entry.state = State::Waiting;
        self.running -= 1;
        Ok(())
    }

    pub fn resume(&mut self, id: &str) -> Result<(), Error> {
        let entry = self.entries.get_mut(id).ok_or(Error::InvalidTransition)?;
        if entry.state != State::Waiting {
            return Err(Error::InvalidTransition);
        }
        entry.state = State::Ready;
        let work = entry.work.clone();
        self.enqueue(&work);
        Ok(())
    }

    /// Remove terminal/cancelled work after its durable transition succeeds.
    /// Cancellation eagerly prunes indices; repeated enqueue/cancel cannot leak.
    pub fn remove(&mut self, id: &str) -> Result<Work, Error> {
        if self
            .entries
            .get(id)
            .is_some_and(|entry| entry.state == State::Reserved)
        {
            return Err(Error::InvalidTransition);
        }
        self.remove_entry(id)
    }

    fn remove_entry(&mut self, id: &str) -> Result<Work, Error> {
        let entry = self.entries.remove(id).ok_or(Error::InvalidTransition)?;
        if entry.state == State::Running {
            self.running -= 1;
        }
        if self
            .session_owners
            .get(&entry.work.session_id)
            .is_some_and(|owner| owner == id)
        {
            self.session_owners.remove(&entry.work.session_id);
        }
        if let Some(order) = self.session_order.get_mut(&entry.work.session_id) {
            order.retain(|queued| queued != id);
            if order.is_empty() {
                self.session_order.remove(&entry.work.session_id);
            }
        }
        for project in &mut self.projects {
            for group in &mut project.groups {
                group.ready.retain(|queued| queued != id);
            }
            project.groups.retain(|group| !group.ready.is_empty());
        }
        self.projects.retain(|project| !project.groups.is_empty());
        Ok(entry.work)
    }

    pub fn state(&self, id: &str) -> Option<State> {
        self.entries.get(id).map(|entry| entry.state)
    }
    pub fn running(&self) -> usize {
        self.running
    }
    pub fn outstanding(&self) -> usize {
        self.entries.len()
    }
    pub fn ready_groups(&self) -> usize {
        self.projects.iter().map(|p| p.groups.len()).sum()
    }

    fn enqueue(&mut self, work: &Work) {
        if !self.projects.iter().any(|p| p.id == work.project_id) {
            self.projects.push_back(Project {
                id: work.project_id.clone(),
                groups: VecDeque::new(),
            });
        }
        let project = self
            .projects
            .iter_mut()
            .find(|p| p.id == work.project_id)
            .expect("project was inserted");
        if !project.groups.iter().any(|g| g.id == work.parent_group) {
            project.groups.push_back(Group {
                id: work.parent_group.clone(),
                ready: VecDeque::new(),
            });
        }
        project
            .groups
            .iter_mut()
            .find(|g| g.id == work.parent_group)
            .expect("group was inserted")
            .ready
            .push_back(work.id.clone());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generation_exhaustion_preserves_existing_reservations_and_indices() {
        let mut queue = FairQueue::new(1, 4).unwrap();
        queue.next_generation = Some(u64::MAX - 1);
        let work = |id: &str| Work {
            id: id.into(),
            project_id: "p".into(),
            parent_group: "g".into(),
            session_id: "s".into(),
        };
        let first = queue.reserve(work("first")).unwrap();
        let last = queue.reserve(work("last")).unwrap();
        let stale = last.clone();
        assert!(matches!(
            queue.reserve(work("overflow")),
            Err(Error::GenerationExhausted)
        ));
        assert_eq!(queue.outstanding(), 2);
        assert_eq!(queue.session_order["s"].len(), 2);
        assert_eq!(queue.ready_groups(), 0);
        queue.commit(last).unwrap();
        assert!(queue.start_next().is_none());
        queue.abort(first).unwrap();
        assert_eq!(queue.start_next().unwrap().id, "last");
        queue.remove("last").unwrap();
        assert!(matches!(
            queue.reserve(work("last")),
            Err(Error::GenerationExhausted)
        ));
        assert!(queue.commit(stale).is_err());
        assert!(queue.entries.is_empty());
        assert!(queue.session_order.is_empty());
        assert!(queue.session_owners.is_empty());
        assert_eq!(queue.ready_groups(), 0);
    }
}
