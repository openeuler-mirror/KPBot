use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Mutex as StdMutex, OnceLock};
use std::time::Duration;
use tokio::sync::mpsc::{channel, Sender};
use tokio::sync::{Mutex, Notify};
use tokio::time::{sleep, timeout};

macro_rules! infof { ($($args:tt)*) => {} }
macro_rules! warnf { ($($args:tt)*) => {} }
type CResult<T> = Result<T, &'static str>;

mod context {
    pub type Context = i64;
    pub fn with_timeout(nanos: i64) -> Context { nanos }
}
mod health {
    #[derive(Default)]
    pub struct CheckRequest {}
}
#[allow(non_snake_case)]
mod CH {
    #[derive(Debug, PartialEq)]
    pub enum NotifyEvent { VmShutdown }
    pub struct CubeHypervisor;
    impl CubeHypervisor {
        pub fn try_wait_notify(&self) -> Result<NotifyEvent, ()> { Err(()) }
    }
}

#[derive(Default)]
struct Transport {
    alive: AtomicBool,
    checks: AtomicUsize,
    reconnects: AtomicUsize,
    business_active: AtomicBool,
    business_started: Notify,
    finish_business: Notify,
    periodic_seen: Notify,
    periodic_overlapped: AtomicBool,
}
impl Transport {
    async fn business_request(&self) {
        assert!(self.alive.load(SeqCst));
        self.business_active.store(true, SeqCst);
        self.business_started.notify_one();
        self.finish_business.notified().await;
        self.business_active.store(false, SeqCst);
    }
}

mod health_ttrpc {
    use super::*;
    pub struct HealthClient(Arc<Transport>);
    impl HealthClient {
        pub fn new(conn: Arc<Transport>) -> Self { Self(conn) }
        pub async fn check(&self, ctx: context::Context, _: &health::CheckRequest) -> CResult<()> {
            if !self.0.alive.load(SeqCst) { return Err("transport closed"); }
            self.0.checks.fetch_add(1, SeqCst);
            if ctx > 100_000_000 {
                self.0.periodic_overlapped.store(self.0.business_active.load(SeqCst), SeqCst);
                self.0.periodic_seen.notify_one();
            }
            Ok(())
        }
    }
}

fn transports() -> &'static StdMutex<HashMap<String, Arc<Transport>>> {
    static REGISTRY: OnceLock<StdMutex<HashMap<String, Arc<Transport>>>> = OnceLock::new();
    REGISTRY.get_or_init(StdMutex::default)
}
struct AsyncUtils;
impl AsyncUtils {
    async fn connect_agent(id: &str) -> CResult<Arc<Transport>> {
        let transport = transports().lock().unwrap().get(id).unwrap().clone();
        transport.reconnects.fetch_add(1, SeqCst);
        if !transport.alive.load(SeqCst) { return Err("CONNECT failed"); }
        Ok(transport)
    }
}

struct Container;
impl Container { async fn notify_vm_shutdown(&self) {} }
enum SandBoxState { Running, Exited }
struct SandBox {
    id: String,
    conn: Option<Mutex<Arc<Transport>>>,
    ch: Option<Arc<Mutex<CH::CubeHypervisor>>>,
    containers: Arc<Mutex<HashMap<String, Container>>>,
    state: Arc<Mutex<SandBoxState>>,
    log: (),
}
impl SandBox {
    fn new(id: &str, transport: Arc<Transport>, installed: bool) -> Self {
        transports().lock().unwrap().insert(id.to_string(), transport.clone());
        Self {
            id: id.to_string(),
            conn: installed.then(|| Mutex::new(transport)),
            ch: Some(Arc::new(Mutex::new(CH::CubeHypervisor))),
            containers: Arc::default(),
            state: Arc::new(Mutex::new(SandBoxState::Running)),
            log: (),
        }
    }

// PATCHED_METHOD
}

#[tokio::test(start_paused = true)]
async fn guest_exits_after_business_success_before_monitor_start() {
    let transport = Arc::new(Transport::default());
    transport.alive.store(true, SeqCst);
    let sandbox = SandBox::new("stale", transport.clone(), true);
    let business = tokio::spawn({ let transport = transport.clone(); async move { transport.business_request().await } });
    transport.business_started.notified().await;
    transport.finish_business.notify_one();
    business.await.unwrap();
    transport.alive.store(false, SeqCst);

    assert!(matches!(sandbox.monitor_vm(false).await, Err("CONNECT failed")));
    assert_eq!(transport.reconnects.load(SeqCst), 1);
}

#[tokio::test(start_paused = true)]
async fn periodic_health_request_overlaps_business_on_reused_transport() {
    let transport = Arc::new(Transport::default());
    transport.alive.store(true, SeqCst);
    let sandbox = SandBox::new("healthy", transport.clone(), true);
    let (stop, monitor) = sandbox.monitor_vm(true).await.unwrap();
    assert_eq!(transport.checks.load(SeqCst), 1, "startup probe");
    assert!(sandbox.conn.as_ref().unwrap().try_lock().is_ok());
    let business = tokio::spawn({ let transport = transport.clone(); async move { transport.business_request().await } });
    transport.business_started.notified().await;

    timeout(Duration::from_secs(70), transport.periodic_seen.notified()).await.unwrap();
    assert!(transport.periodic_overlapped.load(SeqCst));
    assert_eq!(transport.reconnects.load(SeqCst), 0);
    transport.finish_business.notify_one();
    business.await.unwrap();
    stop.send(()).await.unwrap();
    monitor.await.unwrap();
}

#[tokio::test(start_paused = true)]
async fn missing_handle_preserves_original_connection_path() {
    let transport = Arc::new(Transport::default());
    transport.alive.store(true, SeqCst);
    let sandbox = SandBox::new("missing", transport.clone(), false);
    let (stop, monitor) = sandbox.monitor_vm(false).await.unwrap();
    assert_eq!(transport.reconnects.load(SeqCst), 1);
    assert_eq!(transport.checks.load(SeqCst), 0);
    stop.send(()).await.unwrap();
    monitor.await.unwrap();
}
