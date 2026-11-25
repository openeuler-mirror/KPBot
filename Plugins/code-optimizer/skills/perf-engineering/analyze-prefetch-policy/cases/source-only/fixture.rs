// Synthetic source-only fixture; no build or runtime evidence is supplied.
const LOOKAHEAD: usize = 16;

#[repr(C, align(64))]
pub struct Entry {
    pub key: u64,
    pub payload: [u8; 56],
}

#[inline]
unsafe fn prefetch_l1(p: *const Entry) {
    #[cfg(target_arch = "x86_64")]
    core::arch::x86_64::_mm_prefetch(p.cast(), core::arch::x86_64::_MM_HINT_T0);
    #[cfg(target_arch = "aarch64")]
    core::arch::asm!("prfm pldl1keep, [{p}]", p = in(reg) p,
                     options(nostack, preserves_flags, readonly));
}

#[inline]
unsafe fn prefetch_l2(p: *const Entry) {
    #[cfg(target_arch = "x86_64")]
    core::arch::x86_64::_mm_prefetch(p.cast(), core::arch::x86_64::_MM_HINT_T1);
    #[cfg(not(target_arch = "x86_64"))]
    let _ = p;
}

pub unsafe fn harvest_probe(entries: &[Entry]) -> u64 {
    for entry in entries {
        prefetch_l2(entry);
    }
    let mut total = 0_u64;
    for i in 0..entries.len() {
        if i + LOOKAHEAD < entries.len() {
            prefetch_l1(&entries[i + LOOKAHEAD]);
        }
        total = total.wrapping_add(entries[i].key);
    }
    total
}
