// 搜推DAG基准
// DAG: 多路召回(串行) -> 合并 -> 逐条粗排打分 -> qsort全排序取top-K -> 输出checksum
// 目标平台: 鲲鹏 aarch64 (本容器x86仅验证逻辑)
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <cmath>
#include <vector>
#include <string>
#include <algorithm>
#include <chrono>
#include <pthread.h>

static const int K = 100;              // top-K
static const int NUM_CANDIDATES = 1200; // 合并后候选数
static const int RECALL_ROADS = 3;      // 多路召回

struct Candidate {
    uint64_t item_id;
    float score;
    float feats[36];
    char pad[8];
};

// ---------- 各路召回 ----------
struct RoadArg { int road; uint32_t seed; std::vector<Candidate> out; };

static uint32_t xorshift(uint32_t *s) {
    uint32_t x = *s; x ^= x << 13; x ^= x >> 17; x ^= x << 5; *s = x; return x;
}

static void *recall_road(void *p) {
    RoadArg *a = (RoadArg *)p;
    uint32_t s = a->seed;
    a->out.resize(NUM_CANDIDATES);
    for (int i = 0; i < NUM_CANDIDATES; i++) {
        Candidate &c = a->out[i];
        c.item_id = ((uint64_t)a->road << 40) | xorshift(&s);
        for (int f = 0; f < 36; f++) c.feats[f] = (float)(xorshift(&s) % 1000) / 1000.f;
        c.score = 0;
        // 模拟召回侧计算
        for (int f = 0; f < 36; f++) c.score += c.feats[f] * 0.5f;
    }
    return NULL;
}

// ---------- 粗排打分 ----------
static float score_one(const Candidate &c, const float *w) {
    float s = 0;
    for (int f = 0; f < 36; f++) s += c.feats[f] * w[f];
    return s;
}

struct RequestCtx { float w[36]; Candidate all[RECALL_ROADS * NUM_CANDIDATES]; };

static void *worker_thread(void *p) {  // baseline: 每请求新建3个线程
    RequestCtx *ctx = (RequestCtx *)p;
    pthread_t th[RECALL_ROADS];
    RoadArg args[RECALL_ROADS];
    for (int r = 0; r < RECALL_ROADS; r++) {
        args[r].road = r; args[r].seed = 1234u + (uint32_t)ctx->w[0] * 0 + r + 1; // 固定seed保证确定性
        args[r].seed = 1234u + r * 7919u;
        pthread_create(&th[r], NULL, recall_road, &args[r]);
    }
    int n = 0;
    for (int r = 0; r < RECALL_ROADS; r++) {
        pthread_join(th[r], NULL);
        memcpy(ctx->all + n, args[r].out.data(), sizeof(Candidate) * NUM_CANDIDATES);
        n += NUM_CANDIDATES;
    }
    return NULL;
}

static uint64_t process_request(const float *w) {
    RequestCtx *ctx = (RequestCtx *)malloc(sizeof(RequestCtx));
    worker_thread(ctx);
    Candidate *cands = ctx->all;

    // 逐条打分
    for (int i = 0; i < RECALL_ROADS * NUM_CANDIDATES; i++)
        cands[i].score = score_one(cands[i], w);

    // 全排序取 top-K
    std::sort(cands, cands + RECALL_ROADS * NUM_CANDIDATES,
              [](const Candidate &a, const Candidate &b) { return a.score > b.score; });

    uint64_t ck = 0;
    for (int i = 0; i < K; i++)
        ck = ck * 1099511628211ULL + (uint64_t)(cands[i].item_id & 0xFFFFFFFFULL)
             + (uint64_t)(uint32_t)(cands[i].score * 1000.f);
    free(ctx);
    return ck;
}

int main(int argc, char **argv) {
    int warmup = 200, measured = 2000;
    float w[36];
    for (int f = 0; f < 36; f++) w[f] = 0.5f / (1 + f);

    uint64_t last = 0; bool stable = true;
    for (int i = 0; i < warmup; i++) { last = process_request(w); }

    using clk = std::chrono::steady_clock;
    auto t0 = clk::now();
    for (int i = 0; i < measured; i++) {
        uint64_t ck = process_request(w);
        if (ck != last) stable = false;
        last = ck;
    }
    auto t1 = clk::now();
    double sec = std::chrono::duration<double>(t1 - t0).count();
    double qps = measured / sec;
    double rt_us = sec * 1e6 / measured;

    printf("checksum=%lu\n", (unsigned long)last);
    printf("deterministic=%s\n", stable ? "yes" : "no");
    printf("qps=%.1f\n", qps);
    printf("rt_us=%.1f\n", rt_us);
    return stable ? 0 : 2;
}
