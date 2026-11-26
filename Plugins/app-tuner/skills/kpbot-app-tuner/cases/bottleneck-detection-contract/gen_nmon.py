#!/usr/bin/env python3
"""gen_nmon.py — UT 用合成 nmon CSV 生成器（Phase A，collection-decoupling）

经验库 r1 真机数据只覆盖"未触发"侧（均值 4.42%、busy 0），判定分支的触发侧
（整机 CPU 饱和 / iowait / disk busy / memory high / NPU host 高载）无现成数据，
按 kptools-nmon --summary 已验证的真实 CSV 节结构合成。

用法: gen_nmon.py --out FILE [--user 80] [--sys 8] [--wait 0] [--idle 12]
                  [--memtotal 767410] [--memfree 621109] [--cached 55059] [--buffers 11605]
                  [--diskbusy 0] [--cores 4] [--snapshots 2]
"""
import argparse

p = argparse.ArgumentParser()
p.add_argument("--out", required=True)
p.add_argument("--user", type=float, default=80.0)
p.add_argument("--sys", type=float, default=8.0)
p.add_argument("--wait", type=float, default=0.0)
p.add_argument("--idle", type=float, default=12.0)
p.add_argument("--memtotal", type=float, default=767410.0)
p.add_argument("--memfree", type=float, default=621109.0)
p.add_argument("--cached", type=float, default=55059.0)
p.add_argument("--buffers", type=float, default=11605.0)
p.add_argument("--diskbusy", type=float, default=0.0)
p.add_argument("--cores", type=int, default=0)
p.add_argument("--snapshots", type=int, default=2)
a = p.parse_args()

L = []
L.append("AAA,progname,nmon")
L.append("AAA,command,gen_nmon.py (synthetic UT fixture)")
L.append("AAA,cpus,%d" % a.cores)
L.append("CPU_ALL,CPU 1 localhost,User%,Sys%,Wait%,Idle%,Steal%,CPUs")
for i in range(1, a.snapshots + 1):
    L.append("CPU_ALL,T%04d,%.2f,%.2f,%.2f,%.2f,0.00,%d" % (i, a.user, a.sys, a.wait, a.idle, a.cores))
if a.cores > 0:
    for c in range(1, a.cores + 1):
        L.append("CPU%03d,CPU %d localhost,User%%,Sys%%,Wait%%,Idle%%,Steal%%" % (c, c))
        for i in range(1, a.snapshots + 1):
            L.append("CPU%03d,T%04d,%.2f,%.2f,%.2f,%.2f,0.00" % (c, i, a.user, a.sys, a.wait, a.idle))
L.append("MEM,Memory MB localhost,memtotal,hightotal,lowtotal,swaptotal,memfree,highfree,lowfree,swapfree,memshared,cached,active,bigfree,buffers,swapcached,inactive")
for i in range(1, a.snapshots + 1):
    L.append("MEM,T%04d,%.1f,0,0,5120.0,%.1f,0,0,5120.0,0,%.1f,0,0,%.1f,0,0" % (i, a.memtotal, a.memfree, a.cached, a.buffers))
L.append("DISKBUSY,Disk %%Busy localhost,sda")
for i in range(1, a.snapshots + 1):
    L.append("DISKBUSY,T%04d,%.2f" % (i, a.diskbusy))
for i in range(1, a.snapshots + 1):
    L.append("ZZZZ,T%04d,12:00:%02d,19-NOV-2026" % (i, i))

open(a.out, "w").write("\n".join(L) + "\n")
