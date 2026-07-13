//! solver-watchdog — RSS / host memory guardian for Turnstile workers.
//!
//! Modes:
//!   solver-watchdog once [--pid N] [--soft-mb N] [--hard-mb N]
//!   solver-watchdog watch --pid-file PATH --interval-sec N ...
//!
//! On hard limit or critical pressure: SIGTERM → wait → SIGKILL.
//! Writes JSON status to --status-file for the control plane.

use serde::Serialize;
use std::env;
use std::fs;
use std::path::Path;
use std::process;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Debug, Serialize, Clone)]
struct Snapshot {
    ok: bool,
    ts: f64,
    pid: Option<i32>,
    rss_kb: u64,
    rss_mb: f64,
    total_kb: u64,
    available_kb: u64,
    used_kb: u64,
    pressure: u32,
    soft_mb: u64,
    hard_mb: u64,
    recycle: bool,
    action: String,
    message: String,
}

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn read_meminfo() -> (u64, u64) {
    let Ok(text) = fs::read_to_string("/proc/meminfo") else {
        return (0, 0);
    };
    let mut total = 0u64;
    let mut available = 0u64;
    let mut free = 0u64;
    let mut buffers = 0u64;
    let mut cached = 0u64;
    for line in text.lines() {
        let mut parts = line.split_whitespace();
        let key = parts.next().unwrap_or("");
        let val: u64 = parts.next().and_then(|v| v.parse().ok()).unwrap_or(0);
        match key {
            "MemTotal:" => total = val,
            "MemAvailable:" => available = val,
            "MemFree:" => free = val,
            "Buffers:" => buffers = val,
            "Cached:" => cached = val,
            _ => {}
        }
    }
    if available == 0 {
        available = free + buffers + cached;
    }
    (total, available)
}

fn process_rss_kb(pid: i32) -> u64 {
    let path = format!("/proc/{}/status", pid);
    let Ok(text) = fs::read_to_string(path) else {
        return 0;
    };
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("VmRSS:") {
            let kb: u64 = rest
                .split_whitespace()
                .next()
                .and_then(|v| v.parse().ok())
                .unwrap_or(0);
            return kb;
        }
    }
    0
}

fn pressure_score(used_kb: u64, total_kb: u64) -> u32 {
    if total_kb == 0 {
        return 100;
    }
    let r = used_kb as f64 / total_kb as f64;
    ((r * 100.0).round() as u32).min(100)
}

fn should_recycle(rss_kb: u64, soft_mb: u64, hard_mb: u64, pressure: u32) -> bool {
    let soft = soft_mb * 1024;
    let hard = hard_mb * 1024;
    if hard > 0 && rss_kb >= hard {
        return true;
    }
    if soft > 0 && rss_kb >= soft && pressure >= 70 {
        return true;
    }
    if pressure >= 92 {
        return true;
    }
    false
}

fn pid_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    Path::new(&format!("/proc/{}", pid)).exists()
}

fn send_signal(pid: i32, sig: i32) -> bool {
    // libc-free: shell kill is fine for watchdog
    let status = process::Command::new("kill")
        .args([format!("-{}", sig), pid.to_string()])
        .status();
    matches!(status, Ok(s) if s.success())
}

fn read_pid_file(path: &str) -> Option<i32> {
    let text = fs::read_to_string(path).ok()?;
    text.trim().parse().ok()
}

fn write_status(path: Option<&str>, snap: &Snapshot) {
    let Some(p) = path else { return };
    let Ok(body) = serde_json::to_string_pretty(snap) else {
        return;
    };
    let tmp = format!("{}.tmp", p);
    if fs::write(&tmp, body.as_bytes()).is_ok() {
        let _ = fs::rename(&tmp, p);
    }
}

fn snapshot(pid: Option<i32>, soft_mb: u64, hard_mb: u64) -> Snapshot {
    let (total_kb, available_kb) = read_meminfo();
    let used_kb = total_kb.saturating_sub(available_kb);
    let pressure = pressure_score(used_kb, total_kb);
    let rss_kb = pid.map(process_rss_kb).unwrap_or(0);
    let recycle = should_recycle(rss_kb, soft_mb, hard_mb, pressure);
    Snapshot {
        ok: true,
        ts: now_ts(),
        pid,
        rss_kb,
        rss_mb: rss_kb as f64 / 1024.0,
        total_kb,
        available_kb,
        used_kb,
        pressure,
        soft_mb,
        hard_mb,
        recycle,
        action: "none".into(),
        message: String::new(),
    }
}

fn reclaim_host() {
    // Drop page cache is root-only and aggressive; only compact via malloc trim hints
    // by writing to drop_caches is intentionally NOT done (too destructive).
    // Best-effort: notify kernel of free memory via drop_caches is skipped.
    let _ = fs::write("/proc/sys/vm/compact_memory", b"1");
}

fn enforce(mut snap: Snapshot, grace_sec: u64, status_file: Option<&str>) -> Snapshot {
    if !snap.recycle {
        snap.action = "ok".into();
        snap.message = "within limits".into();
        write_status(status_file, &snap);
        return snap;
    }
    let Some(pid) = snap.pid else {
        snap.action = "pressure_only".into();
        snap.message = format!("host pressure={} without target pid", snap.pressure);
        reclaim_host();
        write_status(status_file, &snap);
        return snap;
    };
    if !pid_alive(pid) {
        snap.action = "dead".into();
        snap.message = format!("pid {} already gone", pid);
        write_status(status_file, &snap);
        return snap;
    }
    // Soft: SIGTERM for graceful browser shutdown
    let _ = send_signal(pid, 15);
    snap.action = "sigterm".into();
    snap.message = format!(
        "recycle pid={} rss_mb={:.1} pressure={}",
        pid, snap.rss_mb, snap.pressure
    );
    write_status(status_file, &snap);

    let deadline = now_ts() + grace_sec as f64;
    while now_ts() < deadline {
        if !pid_alive(pid) {
            snap.action = "terminated".into();
            snap.message = format!("pid {} exited after SIGTERM", pid);
            reclaim_host();
            write_status(status_file, &snap);
            return snap;
        }
        thread::sleep(Duration::from_millis(200));
    }
    if pid_alive(pid) {
        let _ = send_signal(pid, 9);
        snap.action = "sigkill".into();
        snap.message = format!("pid {} force-killed after {}s", pid, grace_sec);
    } else {
        snap.action = "terminated".into();
        snap.message = format!("pid {} exited", pid);
    }
    reclaim_host();
    write_status(status_file, &snap);
    snap
}

fn usage() {
    eprintln!(
        "usage:
  solver-watchdog once [--pid N] [--soft-mb N] [--hard-mb N] [--status-file PATH]
  solver-watchdog watch --pid-file PATH [--interval-sec N] [--soft-mb N] [--hard-mb N]
                        [--grace-sec N] [--status-file PATH]
  solver-watchdog version"
    );
}

fn parse_u64(_flag: &str, args: &[String], i: &mut usize, default: u64) -> u64 {
    if *i + 1 < args.len() {
        *i += 1;
        args[*i].parse().unwrap_or(default)
    } else {
        default
    }
}

fn parse_i32(flag: &str, args: &[String], i: &mut usize) -> Option<i32> {
    let _ = flag;
    if *i + 1 < args.len() {
        *i += 1;
        args[*i].parse().ok()
    } else {
        None
    }
}

fn parse_str(args: &[String], i: &mut usize) -> Option<String> {
    if *i + 1 < args.len() {
        *i += 1;
        Some(args[*i].clone())
    } else {
        None
    }
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        usage();
        process::exit(2);
    }
    if args[0] == "version" || args[0] == "-v" {
        println!("solver-watchdog 0.1.0");
        return;
    }

    let mut soft_mb = env::var("SOLVER_WATCHDOG_SOFT_MB")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(900u64);
    let mut hard_mb = env::var("SOLVER_WATCHDOG_HARD_MB")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(1400u64);
    let mut interval = env::var("SOLVER_WATCHDOG_INTERVAL_SEC")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(5u64);
    let mut grace = env::var("SOLVER_WATCHDOG_GRACE_SEC")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8u64);
    let mut pid: Option<i32> = None;
    let mut pid_file: Option<String> = None;
    let mut status_file: Option<String> = None;
    let mut enforce_kill = true;

    let cmd = args[0].as_str();
    let mut i = 1usize;
    while i < args.len() {
        match args[i].as_str() {
            "--pid" => pid = parse_i32("--pid", &args, &mut i),
            "--pid-file" => pid_file = parse_str(&args, &mut i),
            "--soft-mb" => soft_mb = parse_u64("--soft-mb", &args, &mut i, soft_mb),
            "--hard-mb" => hard_mb = parse_u64("--hard-mb", &args, &mut i, hard_mb),
            "--interval-sec" => interval = parse_u64("--interval-sec", &args, &mut i, interval),
            "--grace-sec" => grace = parse_u64("--grace-sec", &args, &mut i, grace),
            "--status-file" => status_file = parse_str(&args, &mut i),
            "--dry-run" => enforce_kill = false,
            other => {
                eprintln!("unknown flag: {}", other);
                usage();
                process::exit(2);
            }
        }
        i += 1;
    }

    match cmd {
        "once" => {
            if pid.is_none() {
                if let Some(ref pf) = pid_file {
                    pid = read_pid_file(pf);
                }
            }
            let snap = snapshot(pid, soft_mb, hard_mb);
            let out = if enforce_kill {
                enforce(snap, grace, status_file.as_deref())
            } else {
                let mut s = snap;
                s.action = if s.recycle {
                    "would_recycle".into()
                } else {
                    "ok".into()
                };
                write_status(status_file.as_deref(), &s);
                s
            };
            println!("{}", serde_json::to_string_pretty(&out).unwrap_or_default());
            process::exit(if out.recycle && out.action == "sigkill" {
                1
            } else {
                0
            });
        }
        "watch" => {
            let pf = pid_file.clone().unwrap_or_default();
            if pf.is_empty() && pid.is_none() {
                eprintln!("watch requires --pid-file or --pid");
                process::exit(2);
            }
            eprintln!(
                "[watchdog] soft={}MB hard={}MB interval={}s grace={}s",
                soft_mb, hard_mb, interval, grace
            );
            loop {
                let mut cur_pid = pid;
                if cur_pid.is_none() {
                    if let Some(ref p) = pid_file {
                        cur_pid = read_pid_file(p);
                    }
                }
                let snap = snapshot(cur_pid, soft_mb, hard_mb);
                if enforce_kill {
                    let _ = enforce(snap, grace, status_file.as_deref());
                } else {
                    write_status(status_file.as_deref(), &snap);
                }
                // if process died after kill, exit so supervisor can restart
                if let Some(p) = cur_pid {
                    if !pid_alive(p) {
                        // wait a bit for pid file refresh
                        thread::sleep(Duration::from_secs(interval.max(1)));
                        let still = if let Some(ref path) = pid_file {
                            read_pid_file(path).filter(|&np| np != p && pid_alive(np))
                        } else {
                            None
                        };
                        if still.is_none() && pid.is_none() {
                            // keep watching pid file for new worker
                            continue;
                        }
                    }
                }
                thread::sleep(Duration::from_secs(interval.max(1)));
            }
        }
        _ => {
            usage();
            process::exit(2);
        }
    }
}
