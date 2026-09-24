use nebula_assistant_runtime::{Error, FairQueue, State, Work};

fn work(id: &str, project: &str, group: &str, session: &str) -> Work {
    Work {
        id: id.into(),
        project_id: project.into(),
        parent_group: group.into(),
        session_id: session.into(),
    }
}

#[test]
fn reservation_capacity_precedes_commit_and_cannot_be_bypassed() {
    let mut queue = FairQueue::new(1, 1).unwrap();
    let first = work("first", "project", "group", "session");
    let ticket = queue.reserve(first.clone()).unwrap();
    assert_eq!(ticket.work_id(), "first");
    assert_eq!(queue.state("first"), Some(State::Reserved));
    assert_eq!(queue.outstanding(), 1);
    assert_eq!(queue.ready_groups(), 0);
    assert_eq!(queue.running(), 0);
    assert!(queue.start_next().is_none());
    assert!(matches!(
        queue.reserve(first.clone()),
        Err(Error::InvalidTransition)
    ));
    assert!(matches!(
        queue.admit(first.clone()),
        Err(Error::InvalidTransition)
    ));
    assert!(matches!(
        queue.reserve(work("first", "other", "group", "session")),
        Err(Error::Conflict)
    ));
    assert!(queue.remove("first").is_err());
    assert!(queue.park("first").is_err());
    assert!(queue.resume("first").is_err());
    let second = work("second", "project", "group", "second-session");
    assert!(matches!(
        queue.reserve(second.clone()),
        Err(Error::Capacity)
    ));
    queue.commit(ticket).unwrap();
    assert!(!queue.admit(first).unwrap());
    assert!(matches!(
        queue.reserve(second.clone()),
        Err(Error::Capacity)
    ));
    assert_eq!(queue.start_next().unwrap().id, "first");
    let second = queue.reserve(second).unwrap();
    // Existing running reserve also covers parking when pending capacity is full.
    queue.park("first").unwrap();
    assert_eq!(queue.outstanding(), 2);
    assert!(matches!(
        queue.reserve(work("third", "p", "g", "s3")),
        Err(Error::Capacity)
    ));
    queue.commit(second).unwrap();
    assert_eq!(queue.start_next().unwrap().id, "second");
    queue.remove("second").unwrap();
    queue.resume("first").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "first");
}

#[test]
fn out_of_order_commits_preserve_session_order_across_projects_and_groups() {
    let mut queue = FairQueue::new(2, 8).unwrap();
    let earlier = queue
        .reserve(work("earlier", "z-project", "last-group", "shared"))
        .unwrap();
    let later = queue
        .reserve(work("later", "a-project", "first-group", "shared"))
        .unwrap();
    let last = queue
        .reserve(work("last", "a-project", "second-group", "shared"))
        .unwrap();
    queue.commit(last).unwrap();
    queue.commit(later).unwrap();
    queue
        .admit(work("peer", "a-project", "first-group", "peer-session"))
        .unwrap();
    assert_eq!(queue.start_next().unwrap().id, "peer");
    assert!(queue.start_next().is_none());
    queue.remove("peer").unwrap();
    queue.commit(earlier).unwrap();
    assert_eq!(queue.start_next().unwrap().id, "earlier");
    assert!(queue.start_next().is_none());
    queue.remove("earlier").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "later");
    assert!(queue.start_next().is_none());
    queue.remove("later").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "last");
    queue.remove("last").unwrap();
    assert_eq!(queue.ready_groups(), 0);
}

#[test]
fn generation_tickets_reject_stale_cross_queue_and_post_commit_operations() {
    let mut first_queue = FairQueue::new(1, 4).unwrap();
    let mut second_queue = FairQueue::new(1, 4).unwrap();
    let item = work("same-id", "p", "g", "session");
    let first = first_queue.reserve(item.clone()).unwrap();
    let duplicate = first.clone();
    let foreign = second_queue.reserve(item.clone()).unwrap();
    assert!(first_queue.commit(foreign.clone()).is_err());
    assert!(first_queue.abort(foreign.clone()).is_err());
    assert!(second_queue.commit(first.clone()).is_err());
    assert_eq!(first_queue.abort(first).unwrap(), item);
    let reused = first_queue.reserve(item.clone()).unwrap();
    assert!(first_queue.commit(duplicate.clone()).is_err());
    assert!(first_queue.abort(duplicate).is_err());
    assert_eq!(first_queue.state("same-id"), Some(State::Reserved));
    first_queue.commit(reused.clone()).unwrap();
    assert!(first_queue.commit(reused.clone()).is_err());
    assert!(first_queue.abort(reused).is_err());
    assert!(matches!(
        first_queue.reserve(item.clone()),
        Err(Error::InvalidTransition)
    ));
    assert!(!first_queue.admit(item.clone()).unwrap());
    assert_eq!(first_queue.start_next().unwrap(), item);
    assert!(!first_queue.admit(item.clone()).unwrap());
    first_queue.park("same-id").unwrap();
    assert!(!first_queue.admit(item).unwrap());
    assert!(first_queue.start_next().is_none());
    second_queue.abort(foreign).unwrap();
    // Retaining a ticket keeps its old queue identity alive, preventing allocator
    // address reuse from ever making it valid in a newly constructed queue.
    let stale = first_queue
        .reserve(work("stale", "p", "g", "other"))
        .unwrap();
    drop(first_queue);
    let mut replacement = FairQueue::new(1, 4).unwrap();
    let current = replacement
        .reserve(work("stale", "p", "g", "other"))
        .unwrap();
    assert!(replacement.commit(stale).is_err());
    replacement.commit(current).unwrap();
}

#[test]
fn abort_and_terminal_removal_prune_session_and_fairness_indices() {
    let mut queue = FairQueue::new(1, 4).unwrap();
    for index in 0..2_000 {
        let session = format!("session-{index}");
        let a = queue.reserve(work("a", &session, "g1", &session)).unwrap();
        let middle = queue
            .reserve(work("middle", &session, "g2", &session))
            .unwrap();
        let z = queue.reserve(work("z", &session, "g3", &session)).unwrap();
        queue.commit(z).unwrap();
        queue.abort(middle).unwrap();
        assert!(queue.start_next().is_none());
        queue.abort(a).unwrap();
        assert_eq!(queue.start_next().unwrap().id, "z");
        queue.remove("z").unwrap();
        assert_eq!(queue.running(), 0);
        assert_eq!(queue.outstanding(), 0);
        assert_eq!(queue.ready_groups(), 0);
    }
    // Invalid work must not allocate an entry or a Session blocker.
    assert!(queue.reserve(work("", "p", "g", "s")).is_err());
    assert_eq!(queue.outstanding(), 0);
    queue.admit(work("new", "p", "g", "s")).unwrap();
    assert_eq!(queue.start_next().unwrap().id, "new");
}

#[test]
fn parked_owner_preserves_order_when_reservations_commit_late() {
    let mut queue = FairQueue::new(1, 8).unwrap();
    queue
        .admit(work("parent", "p", "parent", "shared"))
        .unwrap();
    assert_eq!(queue.start_next().unwrap().id, "parent");
    let next = queue
        .reserve(work("next", "p", "other-group", "shared"))
        .unwrap();
    let last = queue
        .reserve(work("last", "q", "another-group", "shared"))
        .unwrap();
    queue.commit(last).unwrap();
    queue.park("parent").unwrap();
    queue.commit(next).unwrap();
    queue
        .admit(work("child", "p", "parent", "child-session"))
        .unwrap();
    assert_eq!(queue.start_next().unwrap().id, "child");
    queue.remove("child").unwrap();
    assert!(queue.start_next().is_none());
    queue.resume("parent").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "parent");
    queue.remove("parent").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "next");
    queue.park("next").unwrap();
    assert!(queue.start_next().is_none());
    queue.remove("next").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "last");
}
