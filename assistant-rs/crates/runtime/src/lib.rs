//! Bounded assistant scheduling policy. No tools, providers or agents are executed.
//!
//! The owning service must durably admit/claim work before acknowledging/dispatching
//! it. This deterministic policy is deliberately separate from persistence and I/O.

use nebula_assistant_domain::{ValidationError, bounded};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};

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
}

#[derive(Clone)]
struct Entry {
    work: Work,
    state: State,
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
            running: 0,
        })
    }

    /// Returns false for an identical existing admission. There is no durability
    /// guarantee here; the owner must wrap this policy in durable admission.
    pub fn admit(&mut self, work: Work) -> Result<bool, Error> {
        for value in [
            &work.id,
            &work.project_id,
            &work.parent_group,
            &work.session_id,
        ] {
            bounded(value, 200)?;
        }
        if let Some(existing) = self.entries.get(&work.id) {
            return if existing.work == work {
                Ok(false)
            } else {
                Err(Error::Conflict)
            };
        }
        // Waiting work consumes admission capacity. Already-running work can
        // always park; that reserved capacity is bounded by running_limit.
        if self.entries.len() - self.running >= self.pending_limit
            || self.entries.len() >= self.pending_limit + self.running_limit
        {
            return Err(Error::Capacity);
        }
        self.enqueue(&work);
        self.entries.insert(
            work.id.clone(),
            Entry {
                work,
                state: State::Ready,
            },
        );
        Ok(true)
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
                        self.session_owners
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
