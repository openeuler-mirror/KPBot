# 多线程：三个函数、一个顺序、一条全局状态

## 接口

```c
int  kml_fft_init_threads(void);              /* 初始化线程使用，成功返回 1，否则 0；不可重入 */
void kml_fft_plan_with_nthreads(int nthreads);/* 指定 FFT 库接口函数执行的线程数；不可重入 */
void kml_fft_cleanup_threads(void);           /* 释放多线程框架资源；不可重入 */
```

调用顺序（文档明写的约束）：

```
init_threads()  →  plan_with_nthreads(N)  →  建 plan  →  execute  →  ...  →  cleanup_threads()
```

## 三条硬性质

1. **线程数不是 plan 的参数**：`plan_with_nthreads` 是全局设置，只影响**其后新建**的 plan。
   先建 plan 再调它，已建 plan 的线程数不变。
2. **plan 里带着 `num_threads`**（`kml_fft_plan_s` 的字段），所以线程数是「建 plan 那一刻」的快照。
   要让同一个形状跑不同线程数，必须分别设置并各建一次 plan。
3. **cleanup 之后不能再使用多线程功能**；三个函数都**不可重入**，别在多线程竞态区里调用。

## 调优口径

| 现象 | 判定 | 动作 |
|---|---|---|
| 设了线程数但性能没变 | 设置发生在建 plan 之后 | 把 `plan_with_nthreads` 挪到建 plan 之前，plan 重建 |
| 小变换开多线程反而慢 | 并行开销大于收益（与 FFTW 侧同一现象） | 小尺寸用 1 线程，大尺寸再开 |
| 多线程下崩溃/花数 | 忘了 `init_threads` 或提前 `cleanup` | 按顺序补齐；一个进程只 init/cleanup 一次 |
| 每帧重建 plan 想改线程数 | 线程数绑定 plan | 预建 plan 表，键 = 形状 + 线程数 + 变换方向/kind + 布局（别只按形状建键），按场景取用 |

具体拐点（多大尺寸开始开线程、开几个）文档未给数字，**必须在本机实测**；
移植 FFTW 侧的结论时要重新测——两个库的并行粒度不同。

## 与内存/plan 释放的关系

- `cleanup_threads` 只释放多线程框架资源，**不释放 plan 和缓冲**：plan 仍要 `destroy_plan`，缓冲仍要 `kml_fft_free`。
- 进程退出前先 `destroy_plan`/`free`，最后 `cleanup_threads`。
