// C ABI for Go cgo / CLI smoke tests.
#include "solver_util.hpp"
#include <cstdio>
#include <cstdlib>
#include <string>

extern "C" {

int solver_util_pressure_score(uint64_t used_bytes, uint64_t total_bytes) {
  return solver_util::pressure_score(used_bytes, total_bytes);
}

int solver_util_should_recycle(uint64_t rss_bytes, uint64_t soft_mb, uint64_t hard_mb,
                               int pressure) {
  return solver_util::should_recycle(rss_bytes, soft_mb, hard_mb, pressure) ? 1 : 0;
}

int solver_util_token_shape_ok(const char* token, size_t n) {
  return solver_util::token_shape_ok(token, n) ? 1 : 0;
}

uint64_t solver_util_fnv1a64(const char* data, size_t n) {
  return solver_util::fnv1a64(data, n);
}

int solver_util_meminfo(uint64_t* total_kb, uint64_t* available_kb) {
  return solver_util::read_meminfo_kb(total_kb, available_kb) ? 1 : 0;
}

uint64_t solver_util_process_rss_kb(int pid) {
  return solver_util::process_rss_kb(pid);
}

// CLI: solver-util pressure | token <s> | rss <pid>
int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr,
                 "usage: solver-util pressure|token <s>|rss <pid>|recycle <rss_mb> "
                 "<soft> <hard> <pressure>\n");
    return 2;
  }
  std::string cmd = argv[1];
  if (cmd == "pressure") {
    uint64_t total = 0, avail = 0;
    if (!solver_util::read_meminfo_kb(&total, &avail)) {
      std::fprintf(stderr, "meminfo failed\n");
      return 1;
    }
    uint64_t used = total > avail ? (total - avail) : 0;
    int score = solver_util::pressure_score(used * 1024, total * 1024);
    std::printf("{\"total_kb\":%llu,\"available_kb\":%llu,\"used_kb\":%llu,\"pressure\":%d}\n",
                (unsigned long long)total, (unsigned long long)avail,
                (unsigned long long)used, score);
    return 0;
  }
  if (cmd == "token" && argc >= 3) {
    const char* t = argv[2];
    size_t n = std::strlen(t);
    std::printf("{\"ok\":%s,\"len\":%zu}\n",
                solver_util::token_shape_ok(t, n) ? "true" : "false", n);
    return 0;
  }
  if (cmd == "rss" && argc >= 3) {
    int pid = std::atoi(argv[2]);
    uint64_t kb = solver_util::process_rss_kb(pid);
    std::printf("{\"pid\":%d,\"rss_kb\":%llu,\"rss_mb\":%.2f}\n", pid,
                (unsigned long long)kb, kb / 1024.0);
    return 0;
  }
  if (cmd == "recycle" && argc >= 6) {
    uint64_t rss_mb = std::strtoull(argv[2], nullptr, 10);
    uint64_t soft = std::strtoull(argv[3], nullptr, 10);
    uint64_t hard = std::strtoull(argv[4], nullptr, 10);
    int pressure = std::atoi(argv[5]);
    bool yes = solver_util::should_recycle(rss_mb * 1024ull * 1024ull, soft, hard, pressure);
    std::printf("{\"recycle\":%s}\n", yes ? "true" : "false");
    return 0;
  }
  std::fprintf(stderr, "unknown command\n");
  return 2;
}

}  // extern "C"
