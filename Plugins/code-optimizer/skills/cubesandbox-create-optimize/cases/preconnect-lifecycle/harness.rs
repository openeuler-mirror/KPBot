//! Mock transport/VM; the setup and join bodies are extracted from the patch.
#![allow(dead_code)]

use std::collections::HashMap;
use std::future::pending;
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tokio::sync::Notify;
use tokio::time::sleep;

#[derive(Clone, Copy)]
enum Connect { Fail, Pending, Success }
enum Restore { Success, Pending, FailAfterAttempt, FailAfterConnection, Delay(Duration) }
enum PostRestore { Success, Fail, Pending }

struct Probe {
    mode: Connect,
    attempts: AtomicUsize,
    active: AtomicUsize,
    connections: AtomicUsize,
    fallback: AtomicUsize,
    started: Notify,
    connected: Notify,
    post_started: Notify,
}
impl Probe {
    fn new(mode: Connect) -> Arc<Self> {
        Arc::new(Self { mode, attempts: 0.into(), active: 0.into(), connections: 0.into(),
            fallback: 0.into(), started: Notify::new(), connected: Notify::new(), post_started: Notify::new() })
    }
}

struct Client(Arc<Probe>);
impl Client {
    fn new(probe: Arc<Probe>) -> Self {
        probe.connections.fetch_add(1, SeqCst);
        probe.connected.notify_one();
        Self(probe)
    }
}
impl Drop for Client {
    fn drop(&mut self) { self.0.connections.fetch_sub(1, SeqCst); }
}
struct InFlight(Arc<Probe>);
impl Drop for InFlight {
    fn drop(&mut self) { self.0.active.fetch_sub(1, SeqCst); }
}

fn registry() -> &'static Mutex<HashMap<String, Arc<Probe>>> {
    static REGISTRY: OnceLock<Mutex<HashMap<String, Arc<Probe>>>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(HashMap::new()))
}
struct AsyncUtils;
impl AsyncUtils {
    async fn connect_agent(id: &str) -> Result<Client, &'static str> {
        let probe = registry().lock().unwrap().get(id).unwrap().clone();
        probe.attempts.fetch_add(1, SeqCst);
        probe.active.fetch_add(1, SeqCst);
        let _in_flight = InFlight(probe.clone());
        probe.started.notify_one();
        match probe.mode {
            Connect::Fail => Err("connect failed"),
            Connect::Pending => pending().await,
            Connect::Success => Ok(Client::new(probe)),
        }
    }
}

struct SandBox {
    id: String,
    probe: Arc<Probe>,
    restore: Restore,
    post: PostRestore,
    conn: Option<Client>,
}
impl SandBox {
    fn new(mode: Connect, restore: Restore, post: PostRestore) -> Self {
        static IDS: AtomicUsize = AtomicUsize::new(0);
        let id = format!("sandbox-{}", IDS.fetch_add(1, SeqCst));
        let probe = Probe::new(mode);
        registry().lock().unwrap().insert(id.clone(), probe.clone());
        Self { id, probe, restore, post, conn: None }
    }
    async fn start_vm(&mut self) -> Result<(), &'static str> {
        match self.restore {
            Restore::Success => Ok(()),
            Restore::Pending => pending().await,
            Restore::FailAfterAttempt => { self.probe.started.notified().await; Err("restore failed") }
            Restore::FailAfterConnection => { self.probe.connected.notified().await; Err("restore failed") }
            Restore::Delay(delay) => { sleep(delay).await; Ok(()) }
        }
    }
    async fn post_restore(&mut self) -> Result<(), &'static str> {
        self.probe.post_started.notify_one();
        match self.post {
            PostRestore::Success => Ok(()),
            PostRestore::Fail => Err("post-restore failed"),
            PostRestore::Pending => pending().await,
        }
    }
    fn install_agent_conn(&mut self, client: Client) { self.conn = Some(client); }
    async fn connect_agent(&mut self) -> Result<(), &'static str> {
        // This mock checks that fallback does not overlap the cancelled attempt.
        assert_eq!(self.probe.active.load(SeqCst), 0);
        self.probe.fallback.fetch_add(1, SeqCst);
        Err("serial fallback failed")
    }
    async fn create_sandbox(&mut self) -> Result<(), &'static str> {
        // PATCH_SETUP
        let _ = snapshot;
        // Models an existing `?` between restore and connection installation.
        self.post_restore().await?;
        // The runner injects the patch's awaited-result binding and match here.
        // PATCH_JOIN
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::time::advance;

    #[test]
    fn compiled_fixture_does_not_inherit_parent_credentials() {
        for key in ["KPBOT_ENV_SENTINEL_TOKEN", "CI_JOB_TOKEN", "AWS_ACCESS_KEY_ID",
                    "AWS_SECRET_ACCESS_KEY", "DATABASE_URL"] {
            assert!(std::env::var_os(key).is_none(), "inherited credential variable: {key}");
        }
        assert!(std::env::var_os("CARGO_HOME").is_some());
        assert!(std::env::var_os("HOME").is_some());
    }

    async fn settle() { for _ in 0..4 { tokio::task::yield_now().await; } }
    fn launch(mut sandbox: SandBox) -> tokio::task::JoinHandle<Result<(), &'static str>> {
        tokio::spawn(async move { sandbox.create_sandbox().await })
    }

    #[tokio::test(start_paused = true)]
    async fn restore_failure_drains_the_pending_attempt_before_return() {
        let mut sandbox = SandBox::new(Connect::Pending, Restore::FailAfterAttempt, PostRestore::Success);
        assert_eq!(sandbox.create_sandbox().await, Err("restore failed"));
        assert_eq!(sandbox.probe.active.load(SeqCst), 0);
        assert_eq!(sandbox.probe.fallback.load(SeqCst), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn restore_failure_discards_an_already_connected_client() {
        let mut sandbox = SandBox::new(Connect::Success, Restore::FailAfterConnection, PostRestore::Success);
        assert_eq!(sandbox.create_sandbox().await, Err("restore failed"));
        assert_eq!(sandbox.probe.connections.load(SeqCst), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_a_request_cancels_an_inflight_connect() {
        let sandbox = SandBox::new(Connect::Pending, Restore::Pending, PostRestore::Success);
        let probe = sandbox.probe.clone();
        let parent = launch(sandbox);
        probe.started.notified().await;
        parent.abort();
        assert!(parent.await.unwrap_err().is_cancelled());
        settle().await;
        assert_eq!(probe.active.load(SeqCst), 0);
        let attempts = probe.attempts.load(SeqCst);
        advance(Duration::from_secs(35)).await;
        assert_eq!(probe.attempts.load(SeqCst), attempts);
    }

    #[tokio::test(start_paused = true)]
    async fn shorter_parent_request_deadline_cancels_preconnect() {
        let mut sandbox = SandBox::new(Connect::Pending, Restore::Pending, PostRestore::Success);
        let outcome = tokio::time::timeout(Duration::from_millis(5), sandbox.create_sandbox()).await;
        assert!(outcome.is_err());
        settle().await;
        assert_eq!(sandbox.probe.active.load(SeqCst), 0);
        let attempts = sandbox.probe.attempts.load(SeqCst);
        advance(Duration::from_secs(35)).await;
        assert_eq!(sandbox.probe.attempts.load(SeqCst), attempts);
    }

    #[tokio::test(start_paused = true)]
    async fn cancellation_during_backoff_stops_retries_and_id_reuse() {
        let sandbox = SandBox::new(Connect::Fail, Restore::Pending, PostRestore::Success);
        let probe = sandbox.probe.clone();
        let id = sandbox.id.clone();
        let parent = launch(sandbox);
        probe.started.notified().await;
        for delay in [1, 2, 4] {
            advance(Duration::from_millis(delay)).await;
            settle().await;
        }
        let attempts = probe.attempts.load(SeqCst);
        assert!(attempts >= 4);
        advance(Duration::from_millis(1)).await;
        settle().await;
        assert_eq!(probe.attempts.load(SeqCst), attempts, "retries back off instead of spinning every 1ms");
        parent.abort();
        let _ = parent.await;
        settle().await;
        let replacement = Probe::new(Connect::Success);
        registry().lock().unwrap().insert(id, replacement.clone());
        advance(Duration::from_secs(35)).await;
        settle().await;
        assert_eq!(replacement.attempts.load(SeqCst), 0);
        assert_eq!(probe.attempts.load(SeqCst), attempts);
    }

    #[tokio::test(start_paused = true)]
    async fn later_question_mark_exit_cancels_the_owned_task() {
        let mut sandbox = SandBox::new(Connect::Pending, Restore::Delay(Duration::from_millis(1)), PostRestore::Fail);
        assert_eq!(sandbox.create_sandbox().await, Err("post-restore failed"));
        settle().await;
        assert_eq!(sandbox.probe.active.load(SeqCst), 0);
        let attempts = sandbox.probe.attempts.load(SeqCst);
        advance(Duration::from_secs(35)).await;
        assert_eq!(sandbox.probe.attempts.load(SeqCst), attempts);
    }

    #[tokio::test(start_paused = true)]
    async fn request_cancellation_after_restore_also_cleans_up() {
        let sandbox = SandBox::new(Connect::Pending, Restore::Delay(Duration::from_millis(1)), PostRestore::Pending);
        let probe = sandbox.probe.clone();
        let parent = launch(sandbox);
        probe.post_started.notified().await;
        parent.abort();
        let _ = parent.await;
        settle().await;
        assert_eq!(probe.active.load(SeqCst), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn post_restore_timeout_drains_before_serial_fallback() {
        let mut sandbox = SandBox::new(Connect::Pending, Restore::Success, PostRestore::Success);
        assert_eq!(sandbox.create_sandbox().await, Err("serial fallback failed"));
        assert_eq!(sandbox.probe.active.load(SeqCst), 0);
        assert_eq!(sandbox.probe.fallback.load(SeqCst), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn hanging_connect_is_bounded_even_while_restore_is_pending() {
        let sandbox = SandBox::new(Connect::Pending, Restore::Pending, PostRestore::Success);
        let probe = sandbox.probe.clone();
        let parent = launch(sandbox);
        probe.started.notified().await;
        advance(Duration::from_secs(31)).await;
        settle().await;
        assert_eq!(probe.active.load(SeqCst), 0);
        assert_eq!(probe.attempts.load(SeqCst), 1);
        parent.abort();
        let _ = parent.await;
    }

    #[tokio::test(start_paused = true)]
    async fn long_restore_does_not_restart_the_preconnect_budget() {
        let mut sandbox = SandBox::new(Connect::Pending, Restore::Delay(Duration::from_secs(29)), PostRestore::Success);
        let start = tokio::time::Instant::now();
        assert_eq!(sandbox.create_sandbox().await, Err("serial fallback failed"));
        assert_eq!(start.elapsed(), Duration::from_secs(30));
        assert_eq!(sandbox.probe.active.load(SeqCst), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn successful_connection_is_installed_without_fallback() {
        let mut sandbox = SandBox::new(Connect::Success, Restore::Success, PostRestore::Success);
        assert_eq!(sandbox.create_sandbox().await, Ok(()));
        assert!(sandbox.conn.is_some());
        assert_eq!(sandbox.probe.fallback.load(SeqCst), 0);
        assert_eq!(sandbox.probe.active.load(SeqCst), 0);
        sandbox.conn.take();
        assert_eq!(sandbox.probe.connections.load(SeqCst), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn one_hundred_failed_creates_leave_no_retry_tasks() {
        let mut parents = tokio::task::JoinSet::new();
        let mut probes = Vec::new();
        for _ in 0..100 {
            let mut sandbox = SandBox::new(Connect::Pending, Restore::FailAfterAttempt, PostRestore::Success);
            probes.push(sandbox.probe.clone());
            parents.spawn(async move { sandbox.create_sandbox().await });
        }
        while let Some(result) = parents.join_next().await {
            assert_eq!(result.unwrap(), Err("restore failed"));
        }
        assert!(probes.iter().all(|p| p.active.load(SeqCst) == 0));
        let attempts: usize = probes.iter().map(|p| p.attempts.load(SeqCst)).sum();
        advance(Duration::from_secs(35)).await;
        settle().await;
        assert_eq!(probes.iter().map(|p| p.attempts.load(SeqCst)).sum::<usize>(), attempts);
    }
}
