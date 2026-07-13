// Lightweight C++ helpers for hybrid Turnstile stack.
// Pure headers + single TU — no heavy deps. Used by Go via cgo or CLI.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

namespace solver_util {

// Score memory pressure 0..100 from used/total bytes.
inline int pressure_score(uint64_t used_bytes, uint64_t total_bytes) {
  if (total_bytes == 0) return 100;
  double r = static_cast<double>(used_bytes) / static_cast<double>(total_bytes);
  if (r < 0) r = 0;
  if (r > 1) r = 1;
  return static_cast<int>(r * 100.0 + 0.5);
}

// Recommend recycle when RSS exceeds soft/hard MB or pressure high.
inline bool should_recycle(uint64_t rss_bytes, uint64_t soft_mb, uint64_t hard_mb,
                           int pressure) {
  const uint64_t soft = soft_mb * 1024ull * 1024ull;
  const uint64_t hard = hard_mb * 1024ull * 1024ull;
  if (hard > 0 && rss_bytes >= hard) return true;
  if (soft > 0 && rss_bytes >= soft && pressure >= 70) return true;
  if (pressure >= 90) return true;
  return false;
}

// Cheap Turnstile token shape check (not crypto verify).
// CF tokens are typically long base64-ish strings.
inline bool token_shape_ok(const char* token, size_t n) {
  if (!token || n < 20 || n > 4096) return false;
  size_t ok = 0;
  for (size_t i = 0; i < n; ++i) {
    unsigned char c = static_cast<unsigned char>(token[i]);
    if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
        (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
        c == '+' || c == '/' || c == '=') {
      ++ok;
    } else if (c == ' ' || c == '\n' || c == '\r' || c == '\t') {
      return false;
    } else {
      return false;
    }
  }
  return ok == n;
}

// FNV-1a 64 for job id hashing / shard selection.
inline uint64_t fnv1a64(const char* data, size_t n) {
  uint64_t h = 14695981039346656037ull;
  for (size_t i = 0; i < n; ++i) {
    h ^= static_cast<unsigned char>(data[i]);
    h *= 1099511628211ull;
  }
  return h;
}

// Parse /proc/meminfo MemAvailable / MemTotal (Linux). Returns false on failure.
inline bool read_meminfo_kb(uint64_t* total_kb, uint64_t* available_kb) {
  if (!total_kb || !available_kb) return false;
  *total_kb = 0;
  *available_kb = 0;
  FILE* f = std::fopen("/proc/meminfo", "r");
  if (!f) return false;
  char line[256];
  uint64_t mem_total = 0, mem_available = 0, mem_free = 0, buffers = 0, cached = 0;
  while (std::fgets(line, sizeof(line), f)) {
    unsigned long long v = 0;
    if (std::sscanf(line, "MemTotal: %llu", &v) == 1) mem_total = v;
    else if (std::sscanf(line, "MemAvailable: %llu", &v) == 1) mem_available = v;
    else if (std::sscanf(line, "MemFree: %llu", &v) == 1) mem_free = v;
    else if (std::sscanf(line, "Buffers: %llu", &v) == 1) buffers = v;
    else if (std::sscanf(line, "Cached: %llu", &v) == 1) cached = v;
  }
  std::fclose(f);
  *total_kb = mem_total;
  if (mem_available > 0) {
    *available_kb = mem_available;
  } else {
    *available_kb = mem_free + buffers + cached;
  }
  return mem_total > 0;
}

// Read VmRSS of a process (kB). 0 on failure.
inline uint64_t process_rss_kb(int pid) {
  char path[64];
  std::snprintf(path, sizeof(path), "/proc/%d/status", pid);
  FILE* f = std::fopen(path, "r");
  if (!f) return 0;
  char line[256];
  uint64_t rss = 0;
  while (std::fgets(line, sizeof(line), f)) {
    unsigned long long v = 0;
    if (std::sscanf(line, "VmRSS: %llu", &v) == 1) {
      rss = v;
      break;
    }
  }
  std::fclose(f);
  return rss;
}

}  // namespace solver_util
