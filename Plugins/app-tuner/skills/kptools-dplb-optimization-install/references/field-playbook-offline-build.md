# MindIE Motor 源码离线编译实测手册（field playbook）

实测环境：远端容器无外网、无 cargo、无 libzmq 开发包；源码 `MindIE-Motor-3.2.0`（`__version__=3.1.0`，包名 `motor`）。
本手册是 ISS-09~ISS-11 的实测记录，供 skill 的方式 B 在离线/受限环境复用。结论先行：

- **必须 `bash build.sh`，严禁 `pip3 wheel .`**（空壳 whl，见文末根因分析）
- cargo 必需：`libmindie_workload_shm.so` 无 Python 兜底，coordinator 没有它起不来
- kv-conductor 可选：`SKIP_KV_CONDUCTOR_BUILD=1` 跳过，coordinator 仍可启动
- grpcio-tools 版本必须与运行时 grpcio 一致

## 1. 本机下载 Rust 离线安装包

Rust Forge 归档页（**本机经代理可达，远端不可达**）：

```
https://forge.rust-lang.org/infra/archive-stable-version-installers.html
```

实测选用：

```
https://static.rust-lang.org/dist/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz
size   = 344,628,055 bytes
sha256 = 703b190f0d723ae68985797f3bb5f62f01b1723f138464c6f30f32b766eb0abf
```

本机下载后 SHA256 校验一致再上传。

## 2. 分片上传到远端

MCP 单次 upload 300s 超时，实测一次最多传约 285MB（首次 345MB 只传到 284,753,920 字节后超时）。

**续传口径（关键）**：超时后远端残包保留了前 `N` 字节（`N` 以远端 `stat -c %s` / `wc -c` 实测为准，不要拿本地的切分断点想当然）。**不能把"从本地第 0 字节切出的分片"直接追加到残包**——那会重复文件开头，长度和 SHA256 都不可能通过。两种正确做法（任选其一，完成后都必须先核对总字节长度、再核对摘要）：

做法一：按远端已确认的长度切分剩余内容（本机跳过已传前缀，续传省流量）

```bash
# 本机：N 换成远端实测保留字节数，从偏移 N 起把剩余内容切成 20MB/片
python3 -c "import sys; d=open(sys.argv[1],'rb').read(); N=int(sys.argv[2]); r=d[N:]; [open('rust.part%d'%(i+1),'wb').write(r[o:o+20*1024*1024]) for i,o in enumerate(range(0,len(r),20*1024*1024))]" rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz <N>
```

```bash
# 远端：按 part1..partN 顺序追加，随后先验长度再验摘要
cat /tmp/rust.part1 >> /tmp/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz
# ……有几片追加几片，严格按序
wc -c /tmp/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz      # 必须等于源文件字节数（344628055）
sha256sum /tmp/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz   # 必须与下载页一致
```

做法二：从空文件按序重组全部分片（不依赖残包前缀可信，残包异常时首选）

```bash
# 本机：从第 0 字节起全量切分（跨平台，无 split 依赖，按 20MB 一片）
python3 -c "import sys; d=open(sys.argv[1],'rb').read(); [open('rust.part%d'%(i+1),'wb').write(d[o:o+20*1024*1024]) for i,o in enumerate(range(0,len(d),20*1024*1024))]" rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz
```

```bash
# 远端：丢弃残包，从空文件按序重组，再验证长度和摘要
rm -f /tmp/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz
cat /tmp/rust.part1 /tmp/rust.part2 /tmp/rust.part3 > /tmp/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz   # 有几片列几片
wc -c /tmp/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz      # 必须等于源文件字节数（344628055）
sha256sum /tmp/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz   # 必须与下载页一致
```

任一做法完成且长度与 SHA256 双验证通过后，才继续第 3 节的安装步骤。

## 3. 容器内安装 Rust（无需外网）

```bash
mkdir -p /tmp/rust-install
tar -xzf /tmp/rust-1.99.1-aarch64-unknown-linux-gnu.tar.gz -C /tmp/rust-install
cd /tmp/rust-install/rust-1.99.1-aarch64-unknown-linux-gnu
./install.sh --prefix=/usr/local/rust --without=rust-docs --disable-ldconfig
/usr/local/rust/bin/cargo --version    # cargo 1.99.1
/usr/local/rust/bin/rustc --version    # rustc 1.99.1
```

## 4. 离线 vendor `libc` crate

源码没有 `Cargo.lock`，cargo 离线模式解析不到 `libc`（build.sh 的 workload-shm 依赖它）：

```bash
# 本机从 crates.io 下载 libc-0.2.191.crate，上传到容器
mkdir -p /tmp/mm-src/vendor/libc
tar -xzf /tmp/libc-0.2.191.crate -C /tmp/mm-src/vendor/libc --strip-components=1
```

`/tmp/mm-src/vendor/libc/.cargo-checksum.json`（**内容必须从 crates.io 下载时核对的
实际 sha256 提取，不要写死预设值**——上传/解包内容变化时写死值无法检出）：

```bash
# 下载 .crate 后，先核对实际 sha256（crates.io 页面或 python3 -c "import hashlib; ..." 均可），
# 再提取归档内 .cargo-checksum.json（这是 crates.io 构建时生成的逐文件校验，带 files 字典——
# 覆盖每个文件的独立摘要，比手写一行更能检出传输/解包异常）：
tar -xzf /tmp/libc-0.2.191.crate -O .cargo-checksum.json > /tmp/mm-src/vendor/libc/.cargo-checksum.json
# 核对提取的 package 摘要与 crate 整体 sha256 一致（不一致说明解包出错或被篡改，重新上传）：
python3 -c "import json,hashlib; ck=json.load(open('/tmp/mm-src/vendor/libc/.cargo-checksum.json')); sha=hashlib.sha256(open('/tmp/libc-0.2.191.crate','rb').read()).hexdigest(); assert ck.get('package')==sha, f'checksum mismatch: cargo={ck.get(\"package\")[:12]} file={sha[:12]}'"
# 确认 files 字典非空（crates.io 构建包应包含逐文件摘要）：
python3 -c "import json; ck=json.load(open('/tmp/mm-src/vendor/libc/.cargo-checksum.json')); assert len(ck.get('files',{}))>0, 'files 为空——手写预设摘要无法检出逐文件变化，改用完整 crate 归档中的原始 checksum'"
```

备选方案（更完备）：用 `cargo vendor` 生成完整 vendor 目录（在可联网的构建机上执行
`cargo vendor` 后把整个 `vendor/` 目录连同各自的 .cargo-checksum.json 上传），
保留逐文件校验与完整目录结构。

`/tmp/mm-src/.cargo/config.toml`（把 crates.io 替换为本地 vendor 目录）：

```toml
[source.crates-io]
replace-with = "vendored-sources"

[source.vendored-sources]
directory = "vendor"
```

## 5. 重新生成 proto（版本必须匹配）

build.sh 的 `generate_proto.sh` 需要 grpcio-tools。**工具版本必须与运行时 grpcio 一致**——实测运行时 `grpcio 1.82.1`，装 1.85.3 生成的 pb 不匹配：

```bash
pip3 show grpcio                                   # 先查运行时版本
pip3 install --no-deps --force-reinstall grpcio_tools-1.82.1-*.whl
```

## 6. 编译

```bash
cd /tmp/mm-src
export PATH=/usr/local/rust/bin:$PATH
export CARGO_HOME=/usr/local/rust/cargo
SKIP_RUST_INSTALL=1 \
SKIP_KV_CONDUCTOR_BUILD=1 \
CARGO_NET_OFFLINE=true \
bash build.sh
```

实测输出（成功）：

```text
Building workload-shm from source (cargo build --release)...
   Compiling libc v0.2.191
   Compiling mindie-workload-shm v0.1.0
    Finished `release` profile [optimized] target(s) in 1.83s
workload-shm library ready: ./motor/coordinator/workload_shm_rs/lib/libmindie_workload_shm.so
kv-conductor omitted from the wheel (optional).
wheel native lib verified: dist/motor-3.1.0-py3-none-any.whl
wheel package: dist/motor-3.1.0-py3-none-linux_aarch64.whl
BUILD_RC=0
```

## 7. 安装并验证（本 skill 交付终点）

```bash
pip3 install --no-deps --force-reinstall dist/motor-*.whl
cd /opt/mindie-motor    # 关键：切到源码目录之外！
python3 -c "import motor; print(motor.__file__)"   # site-packages/motor/__init__.py
```

**`import motor` 成功并指向 site-packages 即为本 skill 的交付终点**——以下部署侧动作
（启动 coordinator 与探活）不在本 skill 范围：`start_motor.sh` 由配套 PR #443
（kptools-dplb-optimization）的阶段 4 生成，安装场景尚不存在该脚本，此处照做必失败；
探活与管理端注册同样归部署侧流程。

- wheel 内容：含 `motor/coordinator/workload_shm_rs/lib/libmindie_workload_shm.so`（实测 458,712 字节）；不含 `kv-conductor`（可选）。源码构建 wheel 约 1.4MB（官方 aarch64 3.6MB，差额主要是 kv-conductor）
- **`import motor` 必须在源码目录之外执行**：在 `/tmp/mm-src` 内执行会命中源码目录而不是 site-packages，验证安装结果必须切到 `/opt/mindie-motor` 等外部目录
- **运行依赖按上游 `requirements.txt` 全量补装**：本手册实测当时的基础镜像已具备大部分依赖，只补了 pyOpenSSL/watchdog——**新环境不要沿用该最小清单**，coordinator 还硬 import fastapi/pydantic/zmq/msgspec/uvicorn 等，缺任何一个都起不来；离线全量下载见上游 standalone.md 附录 A（`pip download -r requirements.txt`）
- 上面第 123 行的通配 `dist/motor-*.whl` 是当时的一次通装了两个 wheel（`-none-any` 与 `-linux_aarch64`，恰好按字母序平台包最后生效）；正式部署不要沿用——build 可能同时产出两个 wheel，按 SKILL.md 方式 B 第 6 步用第 5 步断言选中的**明确文件名**安装，避免 glob 先后覆盖

## 附：ISS-09 根因——为什么 `pip3 wheel .` 是陷阱

`setup.py` 中 `install_requires=[]`，且只做条件打包：

```python
_kv_bin = os.path.join("motor", "kv_conductor", "bin", "kv-conductor")
if os.path.isfile(_kv_bin):
    _package_data["motor.kv_conductor"] = ["bin/kv-conductor"]
```

`pip wheel` 不会调用 `build.sh`，因此：

1. Rust 组件（`libmindie_workload_shm.so`、`kv-conductor`）不会被编译，wheel 里也没有
2. 实测 `pip3 wheel . --no-deps --no-build-isolation -w dist/` 命令成功、产出 `motor-3.1.0-py3-none-any.whl`（1.2MB）——**命令成功是最大的迷惑性**，装上之后 coordinator 起不来
3. `install_requires=[]` 同时也是运行依赖（pyOpenSSL/watchdog）不被自动安装的根因（ISS-01/ISS-02）

## 附：无 cargo 时的替代路径

- `WORKLOAD_SHM_PREBUILT=<path>/libmindie_workload_shm.so`：用预编译的 .so 直接打包
- `KV_CONDUCTOR_PREBUILT=<path>/kv-conductor`：用预编译的 kv-conductor 二进制直接打包
- 快速探测只缺什么：`SKIP_RUST_INSTALL=1 bash build.sh`——缺 cargo 时立即报
  `[ERROR] refusing to emit dist/motor-*.whl: cargo is required to compile libmindie_workload_shm.so.
   Coordinator cannot start without this library (no Python ledger fallback).`
