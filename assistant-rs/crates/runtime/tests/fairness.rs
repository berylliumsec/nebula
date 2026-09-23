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
fn projects_and_parent_groups_both_make_progress() {
    let mut queue = FairQueue::new(1, 16).unwrap();
    for job in [
        work("a1", "a", "large", "a1"),
        work("a2", "a", "large", "a2"),
        work("small", "a", "small", "small"),
        work("b1", "b", "b", "b1"),
    ] {
        queue.admit(job).unwrap();
    }
    let mut order = Vec::new();
    while let Some(job) = queue.start_next() {
        order.push(job.id.clone());
        queue.remove(&job.id).unwrap();
    }
    assert_eq!(order, ["a1", "b1", "small", "a2"]);
}

#[test]
fn waiting_parent_releases_capacity_but_retains_session_ownership() {
    let mut queue = FairQueue::new(1, 4).unwrap();
    queue
        .admit(work("parent", "p", "parent", "parent-session"))
        .unwrap();
    queue
        .admit(work("followup", "p", "parent", "parent-session"))
        .unwrap();
    queue
        .admit(work("child", "p", "parent", "child-session"))
        .unwrap();
    assert_eq!(queue.start_next().unwrap().id, "parent");
    assert!(queue.start_next().is_none());
    queue.park("parent").unwrap();
    assert_eq!(queue.running(), 0);
    assert_eq!(queue.start_next().unwrap().id, "child");
    queue.remove("child").unwrap();
    assert!(queue.start_next().is_none());
    queue.resume("parent").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "parent");
    queue.remove("parent").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "followup");
}

#[test]
fn skipping_busy_sessions_does_not_reorder_their_followups() {
    let mut queue = FairQueue::new(2, 8).unwrap();
    for (id, session) in [("a0", "a"), ("a1", "a"), ("b0", "b"), ("a2", "a")] {
        queue.admit(work(id, "p", "g", session)).unwrap();
    }
    assert_eq!(queue.start_next().unwrap().id, "a0");
    assert_eq!(queue.start_next().unwrap().id, "b0");
    queue.remove("a0").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "a1");
    queue.remove("a1").unwrap();
    assert_eq!(queue.start_next().unwrap().id, "a2");
}

#[test]
fn duplicate_admission_is_idempotent_and_does_not_bypass_capacity() {
    let mut queue = FairQueue::new(1, 1).unwrap();
    let original = work("one", "p", "g", "s");
    assert!(queue.admit(original.clone()).unwrap());
    assert!(!queue.admit(original).unwrap());
    assert!(matches!(
        queue.admit(work("two", "p", "g", "s2")),
        Err(Error::Capacity)
    ));
    assert!(matches!(
        queue.admit(work("one", "other", "g", "s")),
        Err(Error::Conflict)
    ));
}

#[test]
fn parked_work_always_fits_its_reserved_capacity() {
    let mut queue = FairQueue::new(1, 1).unwrap();
    queue.admit(work("one", "p", "g", "s1")).unwrap();
    queue.start_next().unwrap();
    queue.admit(work("two", "p", "g", "s2")).unwrap();
    queue.park("one").unwrap();
    assert_eq!(queue.outstanding(), 2);
    assert!(matches!(
        queue.admit(work("three", "p", "g", "s3")),
        Err(Error::Capacity)
    ));
    assert_eq!(queue.start_next().unwrap().id, "two");
}

#[test]
fn cancellation_prunes_indices_and_releases_waiting_session() {
    let mut queue = FairQueue::new(1, 2).unwrap();
    for i in 0..10_000 {
        let id = i.to_string();
        queue.admit(work(&id, &id, &id, &id)).unwrap();
        queue.remove(&id).unwrap();
    }
    assert_eq!(queue.outstanding(), 0);
    assert_eq!(queue.ready_groups(), 0);
    queue.admit(work("parent", "p", "g", "s")).unwrap();
    queue.start_next().unwrap();
    queue.park("parent").unwrap();
    queue.remove("parent").unwrap();
    queue.admit(work("new", "p", "g", "s")).unwrap();
    assert_eq!(queue.start_next().unwrap().id, "new");
}

#[test]
fn invalid_transitions_cannot_duplicate_execution_slots() {
    let mut queue = FairQueue::default();
    queue.admit(work("one", "p", "g", "s")).unwrap();
    assert!(queue.park("one").is_err());
    assert!(queue.resume("one").is_err());
    queue.start_next().unwrap();
    queue.park("one").unwrap();
    assert!(queue.park("one").is_err());
    queue.resume("one").unwrap();
    assert!(queue.resume("one").is_err());
    assert_eq!(queue.state("one"), Some(State::Ready));
    assert_eq!(queue.running(), 0);
    queue.start_next().unwrap();
    assert!(queue.start_next().is_none());
    assert_eq!(queue.running(), 1);
}

#[test]
fn one_hundred_sessions_progress_under_a_small_execution_limit() {
    let mut queue = FairQueue::new(8, 200).unwrap();
    for id in 0..100 {
        queue
            .admit(work(
                &format!("w{id}"),
                &format!("p{}", id % 5),
                &format!("g{}", id % 10),
                &format!("s{id}"),
            ))
            .unwrap();
    }
    let mut completed = std::collections::HashSet::new();
    while queue.outstanding() > 0 {
        let mut running = Vec::new();
        while let Some(job) = queue.start_next() {
            running.push(job);
        }
        assert!(!running.is_empty() && running.len() <= 8);
        for job in running {
            assert!(completed.insert(job.id.clone()));
            queue.remove(&job.id).unwrap();
        }
    }
    assert_eq!(completed.len(), 100);
}
