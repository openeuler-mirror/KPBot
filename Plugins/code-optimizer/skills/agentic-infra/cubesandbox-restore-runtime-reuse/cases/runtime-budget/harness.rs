use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Barrier, Mutex};
use std::thread;
use std::time::Duration;
use tokio::task::JoinHandle;

macro_rules! debug { ($($args:tt)*) => {} }
macro_rules! anyhow { ($($args:tt)*) => { format!($($args)*) } }
const MAX_WORKER_THREADS: usize = 5;
static LIMIT: AtomicUsize = AtomicUsize::new(3);
static RESIDENT: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);
static BUILDS: AtomicUsize = AtomicUsize::new(0);

// Enforce an explicit worker quota at the builder boundary. Successful builds
// and device tasks execute on a real Tokio pool.
mod tokio {
    pub use ::tokio::{spawn, task};
    pub mod runtime {
        use crate::{BUILDS, LIMIT, PEAK, RESIDENT, SeqCst};
        use std::future::Future;
        use std::io;

        pub struct Builder { workers: usize }
        impl Builder {
            pub fn new_multi_thread() -> Self { Self { workers: 1 } }
            pub fn worker_threads(&mut self, workers: usize) -> &mut Self {
                self.workers = workers;
                self
            }
            pub fn enable_all(&mut self) -> &mut Self { self }
            pub fn build(&mut self) -> io::Result<Runtime> {
                let workers = self.workers;
                let previous = RESIDENT.fetch_update(SeqCst, SeqCst, |current| {
                    let next = current + workers;
                    (next <= LIMIT.load(SeqCst)).then_some(next)
                }).map_err(|_| io::Error::new(io::ErrorKind::Other, "worker quota exhausted"))?;
                let pool = match ::tokio::runtime::Builder::new_multi_thread().worker_threads(workers).enable_all().build() {
                    Ok(pool) => pool,
                    Err(error) => { RESIDENT.fetch_sub(workers, SeqCst); return Err(error); }
                };
                PEAK.fetch_max(previous + workers, SeqCst);
                BUILDS.fetch_add(1, SeqCst);
                Ok(Runtime { pool: Some(pool), workers })
            }
        }
        pub struct Runtime { pool: Option<::tokio::runtime::Runtime>, workers: usize }
        impl Runtime {
            pub fn block_on<F: Future>(&self, future: F) -> F::Output {
                self.pool.as_ref().unwrap().block_on(future)
            }
        }
        impl Drop for Runtime {
            fn drop(&mut self) {
                drop(self.pool.take());
                RESIDENT.fetch_sub(self.workers, SeqCst);
            }
        }
    }
}

#[derive(Debug)]
enum MigratableError { Restore(String) }
struct DeviceNode { id: String, migratable: Option<Arc<Mutex<Device>>> }
struct Snapshot { snapshots: HashMap<String, Box<usize>> }
enum Device {
    Record(Arc<Mutex<Vec<usize>>>),
    Fail(Arc<Barrier>),
    Finish(Arc<Barrier>, Arc<AtomicBool>),
}
impl Device {
    fn pause(&mut self) -> Result<(), MigratableError> { Ok(()) }
    fn restore(&mut self, marker: usize) -> Result<(), MigratableError> {
        match self {
            Self::Record(output) => output.lock().unwrap().push(marker),
            Self::Fail(barrier) => {
                barrier.wait();
                return Err(MigratableError::Restore("device failed".into()));
            }
            Self::Finish(barrier, completed) => {
                barrier.wait();
                thread::sleep(Duration::from_millis(25));
                completed.store(true, SeqCst);
            }
        }
        Ok(())
    }
}
struct DeviceManager;
impl DeviceManager {
// PATCHED_METHOD
}

fn node(id: &str, device: Device) -> DeviceNode {
    DeviceNode { id: id.into(), migratable: Some(Arc::new(Mutex::new(device))) }
}
fn layout(count: usize) -> Vec<DeviceNode> {
    (0..count).map(|_| DeviceNode { id: "unused".into(), migratable: None }).collect()
}
fn snapshot(entries: &[(&str, usize)]) -> Snapshot {
    Snapshot { snapshots: entries.iter().map(|(id, value)| (id.to_string(), Box::new(*value))).collect() }
}

#[test]
fn switches_layout_within_quota_and_recovers_after_build_failure() {
    let mut manager = DeviceManager;
    let restored = Arc::new(Mutex::new(Vec::new()));
    for (count, marker) in [(2, 1), (2, 2), (3, 3)] {
        let mut devices = layout(count - 1);
        devices.push(node("tracked", Device::Record(restored.clone())));
        manager.restore_device_node((devices, vec![]), snapshot(&[("tracked", marker)])).unwrap();
    }
    assert_eq!(*restored.lock().unwrap(), vec![1, 2, 3]);
    assert_eq!(BUILDS.load(SeqCst), 2, "matching layout must reuse the pool");
    assert_eq!(PEAK.load(SeqCst), 3, "resident workers never oversubscribe the quota");

    let completed = Arc::new(AtomicBool::new(false));
    let barrier = Arc::new(Barrier::new(2));
    let failure = manager.restore_device_node((
        vec![node("fail", Device::Fail(barrier.clone())), node("finish", Device::Finish(barrier, completed.clone()))],
        vec![node("later", Device::Record(restored.clone()))],
    ), snapshot(&[("fail", 1), ("finish", 2), ("later", 4)])).unwrap_err();
    assert!(matches!(failure, MigratableError::Restore(reason) if reason == "device failed"));
    assert!(completed.load(SeqCst), "running sibling must finish before error returns");
    assert_eq!(*restored.lock().unwrap(), vec![1, 2, 3], "later group must not start");

    LIMIT.store(1, SeqCst);
    let error = manager.restore_device_node((layout(2), vec![]), snapshot(&[])).unwrap_err();
    assert!(matches!(error, MigratableError::Restore(reason) if reason.contains("worker quota exhausted")));
    assert_eq!(RESIDENT.load(SeqCst), 0, "failed build may not retain the retired pool");
    manager.restore_device_node((layout(1), vec![]), snapshot(&[])).unwrap();
    assert_eq!(RESIDENT.load(SeqCst), 1);
}
