# torch_npu Wheel 打包 Playbook（setuptools≥84 场景）

> 来源：DeepSeek-V4-Flash / DSV4 用例 A+K 场景（鲲鹏 + 昇腾，P/D 多机）实测。毕昇 GCC/BiSheng 重编 torch_npu PGO2 时，**官方源码在 setuptools≥84.0.0 下打包会静默失败**，产出残缺 whl；本文给出根因、修复、多机一致性与验证方法。

## 适用信号

- `torch_npu`（PyTorch → CANN 适配层）用 `bash ci/build.sh --enable_lto --enable_pgo=2` 重编 PGO2，产物 whl **缺失** `csrc/`、`include/third_party/`、`torchnpugen/`，或目录层级错乱。
- `setup.py build_py` 报 `error: package directory 'build/packages/torch_npu' does not exist` 或 `'build/packages/torchnpugen' does not exist`。
- 多机（P/D）同源码重编后 `import torch_npu` / wheel 内容不一致。
- 安装的 `torch_npu.__version__` 是 `gitUnknown`（源码目录缺 `.git`）或 `git5dd8ef3` 与实际不符。

## 根因：setuptools 84.0.0 绕过自定义 `build_py.run()`

官方 `torch_npu/setup.py` 靠自定义类把头文件/ACL 头/包目录写进构建配置：

```python
class PythonPackageBuild(build_py, object):
    def run(self) -> None:
        ret = get_src_py_and_dst()      # 生成 csrc/*.h、third_party/acl/inc 等打包清单 + 创建 build/packages/* 目录
        for src, dst in ret:
            self.copy_file(src, dst)
        super(PythonPackageBuild, self).finalize_options()   # 旧版：只调 finalize_options，不收集打包内容
```

setuptools **84.0.0** 改了 `bdist_wheel` 流程：直接调用 `build_py`，**绕过了 `PythonPackageBuild.run()`**（只走 `finalize_options()` 或直接由父类 run 处理）。于是 `get_src_py_and_dst()` 产出的 csrc 头 / ACL 头 / 包目录**根本没被触发**，官方 build.sh 直接产出的 whl 天然缺失这些内容。

## 修复：在 `BdistWheelBuild.run()` 开头强制生成包目录

关键：setuptools 84 确实会调用 `BdistWheelBuild.run()`（作为 `cmdclass['bdist_wheel']`），所以把 `get_src_py_and_dst()` 提前到它开头：

```python
class PythonPackageBuild(build_py, object):
    def run(self) -> None:
        ret = get_src_py_and_dst()
        for src, dst in ret:
            self.copy_file(src, dst)
        super(PythonPackageBuild, self).run()      # 修复1：run() 而非 finalize_options()

class BdistWheelBuild(bdist_wheel):
    def run(self):
        # 修复2（关键）：setuptools84 下真正被调用，强制先生成包目录
        ret = get_src_py_and_dst()
        for src, dst in ret:
            self.copy_file(src, dst)
        if which('patchelf') is not None:
            patchelf_dynamic_library()
        # ... 其余默认逻辑 ...
        bdist_wheel.run(self)
```

应用方式（幂等，用 python 定位替换）：

```bash
SRC=/path/to/torch_npu_src
cp $SRC/setup.py $SRC/setup.py.bak_$(date +%Y%m%d_%H%M%S)
# 修复1: finalize_options -> run
sed -i 's/super(PythonPackageBuild, self).finalize_options()/super(PythonPackageBuild, self).run()/' $SRC/setup.py
# 修复2: 在 BdistWheelBuild.run() 里第一个 if which('patchelf') 前插入 get_src_py_and_dst()
grep -n "class BdistWheelBuild" $SRC/setup.py   # 拿到行号后按需插入
```

> 幂等性：`get_src_py_and_dst()` 内部用 `os.makedirs(..., exist_ok=True)`，重复调用安全。但不要在 `PythonPackageBuild.run()` 和 `BdistWheelBuild.run()` 都触发后重复 copy_file 造成 back-up；实测多跑一次无副作用。

## whl 内容契约（正确的 PGO2 官方构建）

修复后产出的是**纯官方 build**（不再需要手工 repack），whl 内容与「手工 repack 的 A 线」有本质区别：

| 项 | 正确（官方 B 线，PGO2） | 残缺/手工 repack（A 线） |
|---|---|---|
| 顶层结构 | `torch_npu/` + `torchnpugen/` + `.dist-info` + `.egg-info` | 同前 |
| `torch_npu/csrc/` | ✅ 有（~681 条目，正确位置） | ❌ 缺失或只有 ~4 个（在 torchair 深层） |
| `torch_npu/include/third_party/`（ACL/HCCL 头） | ✅ 有（~74 条目） | ❌ 0 个 |
| csrc/aten 源文件位置 | ✅ `torch_npu/csrc/...` | ❌ 错放 `include/torch_npu/aten/*.cpp` |
| 总条目 | ~2398 | ~2630（多出的把源码塞进 include） |

**whl 条目数 2398 / csrc 681 / torchnpugen 57** 可作为 PGO2 官方构建的指纹，跨机校验用。

**`ci/build.sh` 用 `build_py` 还是 `build`**：官方默认 `setup.py build_clib build_ext build bdist_wheel` 用 `build`；若被改成 `build_py bdist_wheel`（某些 repack 脚本），会跳过 `build_clib/build_ext`（CMake 原生编译），日志只有几十行无 `[n/m] Building` 记录 → 报 `build/packages/torch_npu does not exist`。应改回 `build_clib build_ext build bdist_wheel`。

## build_py 阶段 `torchnpugen does not exist`（增量 vs 全新构建）

- **现象**：全新 `rm -rf build` 后，`setup.py build bdist_wheel` 在 `running build_py` 阶段报 `package directory 'build/packages/torchnpugen' does not exist`，即使 `BdistWheelBuild` 修复已就位。
- **原因**：`BdistWheelBuild.run()`（含 `get_src_py_and_dst()`）在 `bdist_wheel` 阶段才触发，但 `build` 命令内部的 `build_py` **先跑**且需要目录已存在。D 的 B 线之所以成功，是走**增量打包**（increpkg）——`build/packages/torchnpugen` 已在磁盘残留，build_py 能读到。
- **解法（全新构建）**：在跑 setup.py 前**预创建包目录**，再增量 `setup.py build bdist_wheel`（保留已编译对象，ninja 检测 up-to-date 不重编）：

```bash
mkdir -p $SRC/build/packages/torch_npu $SRC/build/packages/torchnpugen
# 全量编译完（[n/m] Building 到 100% + libtorch_npu.so 链接完成后）再增量打包：
python3.12 setup.py build bdist_wheel
```

- 时间线：全新 `ci/build.sh --enable_lto --enable_pgo=2` 先做完 CMake/ninja 编译（1200+ Building 记录、libtorch_npu.so 118MB 链接成功），更新脚本后再 `setup.py build bdist_wheel` 打包。**不要再次 `rm -rf build`**，会丢掉已编译对象。

## 多机一致性（P/D）

- P/D 同源码重编，**二进制 md5 不同是正常的**（各自容器编译环境/优化细节不同），只要：
  - `torch_npu.__version__ == 2.10.0.post4+git5dd8ef3`（git 哈希一致）
  - whl 条目数一致（2398 / csrc 681 / torchnpugen 57）
  - 容器内 `import torch_npu` + `torch_npu.npu.is_available()` 均 OK
- 不要假设「一台通过 = 另一台 OK」，必须**逐节点** `import` + `md5sum` + whl 条目校验。
- 安装用 `pip install --force-reinstall --no-deps <指定新whl路径>`，别用 `ls dist/*.whl | head -1`（会按字母序选到旧的，如 git449b176 而非新编 gitunknown）；用 `cat <site-packages>/torch-*.dist-info/direct_url.json` 复核实际安装来源。
- `/home`、`/data` 常是 host bind-mount（构建产物/profile 安全）；`/usr/local`（系统 python、sitecustomize、已装 whl）可能容器本地层——容器被 `SIGKILL`(ExitCode=137) 后用 `docker start <cid>` 重启同一容器保留本地层，勿重建；`docker inspect -f '{{.State.Status}} {{.State.OOMKilled}} {{.State.FinishedAt}}' <cid>` 判断退出原因。

## 验证清单

```bash
# 1. 版本与二进制
python -c "import torch_npu; print(torch_npu.__version__)"          # 期望 2.10.0.post4+git5dd8ef3
md5sum <site-packages>/torch_npu/lib/libtorch_npu.so                 # 需为本次 PGO2 产物

# 2. whl 内容指纹（正式构建应 2398 条目 / csrc 681 / torchnpugen 57）
python3 -c "
import zipfile
z=zipfile.ZipFile('/path/torch_npu-*.whl')
n=z.namelist()
print('total', len(n), 'csrc', sum(1 for x in n if '/csrc/' in x), 'tnugen', sum(1 for x in n if x.startswith('torchnpugen')))
"

# 3. 容器内功能（需 CANN env + libomp）
source /usr/local/Ascend/cann-9.1.0/set_env.sh
LD_LIBRARY_PATH=$PY312/lib:/usr/local/lib:/usr/lib64 python -c "
import torch, torch_npu
print('torch_npu', torch_npu.__version__)
print('npu', torch_npu.npu.is_available())
print('matmul', (torch.randn(4,4).npu() @ torch.randn(4,4).npu()).shape)
"

# 4. PGO2 确认：编译命令含 -fprofile-use + -DPGO_MODE=2
grep -m1 'fprofile-use=' $LOG | cut -c1-80     # 若日志无该串，看 ninja 默认不打印完整命令行，用 ps 实时抓 clang++ 参数
grep -o 'PGO_MODE=2' $LOG | head -1
```
