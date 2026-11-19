#!/usr/bin/env node
// 固定开发 fixture 的端到端验收，不是用户业务训练数据。
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { execute } from '../scripts/pgo.mjs';
import { fileHash, writeJSON, readJSON } from '../scripts/lib/contract.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
if (args.length && !(args.length === 2 && args[0] === '--output' && path.isAbsolute(args[1]))) {
  console.error('Usage: node tests/acceptance.mjs [--output <new-absolute-directory>]');
  process.exit(2);
}
const output = args[1] ?? path.join(here, '.acceptance-output', `${Date.now()}-${process.pid}`);
if (fs.existsSync(output)) throw new Error('Output must be a new directory; old evidence is never overwritten.');
fs.mkdirSync(output, { recursive: true });
const results = [];
const supported = process.platform === 'linux' && process.arch === 'arm64';
const stages = ['init', 'baseline', 'instrument', 'train', 'check-profile', 'optimize', 'verify', 'compare'];

const cmake = `cmake_minimum_required(VERSION 3.24)
project(pgo_acceptance LANGUAGES C CXX)
find_package(Threads REQUIRED)
add_library(kernel kernel.c)
add_executable(driver driver.cpp)
target_link_libraries(driver PRIVATE kernel Threads::Threads)
target_compile_features(driver PRIVATE cxx_std_17)
add_library(other SHARED kernel.c)
add_executable(rogue driver.cpp)
target_link_libraries(rogue PRIVATE other Threads::Threads)
target_compile_features(rogue PRIVATE cxx_std_17)
add_dependencies(driver rogue)
`;
const kernel = `#include <stdint.h>
uint64_t calculate(uint64_t x) {
  if(x < 32) return 3*x+7;
  if(x < 256) return x*x+11;
  return (x ^ 0x5a5a)+13;
}
`;
const driver = `#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>
#include <vector>
extern "C" uint64_t calculate(uint64_t);
uint64_t reference(uint64_t x) {return x>=256 ? (x^0x5a5a)+13 : x>=32 ? x*x+11 : 3*x+7;}
int main(int argc,char** argv) {
  if(argc!=4) return 2;
  std::ifstream input(argv[1]);
  if(!input) return 3;
  std::vector<uint64_t> values; uint64_t x;
  while(input>>x) values.push_back(x);
  if(values.empty() || !input.eof()) return 4;
  unsigned long loops; int threads;
  try {loops=std::stoul(argv[2]); threads=std::stoi(argv[3]);} catch(...) {return 5;}
  if(!loops || loops>10000000 || (threads!=1 && threads!=2)) return 6;
  for(auto v:values) if(calculate(v)!=reference(v)) return 7;
  auto work=[&](){volatile uint64_t sum=0; for(unsigned long i=0;i<loops;i++) for(auto v:values) sum+=calculate(v);return uint64_t(sum);};
  uint64_t a=0,b=0; auto start=std::chrono::steady_clock::now();
  if(threads==2) {std::thread first([&]{a=work();}),second([&]{b=work();});first.join();second.join();}
  else a=work();
  auto ns=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
  uint64_t expected=0; for(auto v:values) expected+=reference(v);
  if(a+b!=expected*loops*threads) return 8;
  std::cout<<"{\\"correct\\":true,\\"metrics\\":{\\"elapsed_ns\\":"<<ns<<"},\\"checksum\\":"<<a+b<<"}\\n";
  std::cout.flush();
  if(std::getenv("ACCEPTANCE_NO_FLUSH")) std::_Exit(0);
  return 0;
}
`;

function tool(name) {
  for (const dir of (process.env.PATH ?? '').split(path.delimiter)) {
    const candidate = path.join(dir, name);
    try { fs.accessSync(candidate, fs.constants.X_OK); if (fs.statSync(candidate).isFile()) return candidate; } catch {}
  }
  // 缺失工具交给真实执行器的环境门控，不安装也不换工具链。
  return path.resolve('/usr/bin', name);
}
function fixture(id, shared = false, threaded = false) {
  const base = path.join(output, id), source = path.join(base, 'source');
  fs.mkdirSync(source, { recursive: true });
  for (const [name, text] of Object.entries({'CMakeLists.txt':cmake,'kernel.c':kernel,'driver.cpp':driver})) fs.writeFileSync(path.join(source,name),text);
  const datasets = [['train','1 2 3 7 31 32 100 255 256 1000\n'],['validate','4 6 8 13 30 50 127 200 257 4096\n']].map(([name,text])=>{
    const file=path.join(base,`${name}.txt`);fs.writeFileSync(file,text);
    return {id:name,path:file,sha256:fileHash(file),description:'固定开发验收输入，不代表业务',user_evidence:'用户要求编写验收用例；运行本测试即选择固定 fixture'};
  });
  const threads=threaded?'2':'1', library=shared?'libkernel.so':'libkernel.a';
  return {
    version:1,authorization:{explicit_pgo:true,user_request:'执行固定开发 fixture 的 PGO 验收'},approval:{confirmed:true,evidence:'显式运行验收脚本，接受脚本中固定的开发测试协议'},
    source_dir:source,run_dir:path.join(base,'experiment'),
    environment:{platform:'linux',arch:'arm64',hostname:os.hostname(),cpu_scope:'当前验收节点',isolation:'开发验收；性能不作业务承诺'},
    tools:{cc:tool('gcc'),cxx:tool('g++'),cmake:tool('cmake'),gcov_tool:tool('gcov-tool'),gcov_dump:tool('gcov-dump')},
    build:{generator:'Unix Makefiles',type:'Release',targets:['kernel','driver'],link_targets:shared?['kernel','driver']:['driver'],configure_args:[`-DBUILD_SHARED_LIBS=${shared?'ON':'OFF'}`]},
    workload:{process_model:threaded?'joined_threads':'single',description:'有限整数运算，逐项正确性检查，线程正常 join',env:{}},datasets,
    training:[{id:'train',argv:['{build}/driver','{data:train}','10000',threads],dataset_ids:['train'],description:'固定开发训练分布',runs:1}],
    validation:[{id:'heldout',argv:['{build}/driver','{data:validate}','10000',threads],dataset_ids:['validate'],description:'独立固定输入',weight:1,max_regression_pct:5}],
    correctness:{argv:['{build}/driver','{data:validate}','1',threads],description:'逐项比较及完整 checksum 检查'},
    measurement:{metric:'elapsed_ns',direction:'lower',pairs:6,warmups:1,min_gain_pct:1,max_cv_pct:10,bootstrap_samples:1000,seed:123,confidence:0.95},
    resources:{jobs:2,timeout_seconds:120,total_seconds:1800,min_free_bytes:10485760},artifacts:['driver',library],
    libraries:[{target:'kernel',path:library,kind:shared?'shared':'static',...(shared?{}:{link_target:'driver'})}],
  };
}
function stage(c,name) {writeJSON(path.join(path.dirname(c.source_dir),'contract.json'),c);return execute(name,c);}
function through(c,last) {for(const name of stages){stage(c,name);if(name===last)break;}}
function rejected(fn, code) {assert.throws(fn,e=>e.code===code,`Expected rejection code ${code}`);}
function check(id,fn,needsArm=true) {
  if(needsArm&&!supported){results.push({id,status:'skipped',reason:'requires_linux_arm64'});return;}
  try{fn();results.push({id,status:'passed'});}catch(e){results.push({id,status:'failed',code:e.code??'assertion',message:e.message});}
  console.log(`${id}: ${results.at(-1).status}`);
  writeJSON(path.join(output,'results.json'),results);
}
check('missing-training',()=>{const c=fixture('missing-training');c.training=[];rejected(()=>stage(c,'init'),'input_gate');assert.equal(fs.existsSync(c.run_dir),false);},false);
check('missing-authorization',()=>{const c=fixture('missing-authorization');c.authorization.explicit_pgo=false;rejected(()=>stage(c,'init'),'input_gate');assert.equal(fs.existsSync(c.run_dir),false);},false);
for(const [id,shared,threaded] of [['static',false,false],['shared',true,false],['static-threads',false,true],['shared-threads',true,true]]) {
  check(id,()=>{const c=fixture(id,shared,threaded);through(c,'compare');const r=stage(c,'report').pgo_result;assert.equal(r.execution_status,'completed');assert.ok(['improved','no_gain','regressed','inconclusive'].includes(r.optimization_outcome));assert.ok(fs.existsSync(path.join(c.run_dir,'profile-manifest.json')));assert.equal(readJSON(path.join(c.run_dir,'measurements.json')).heldout.length,6);});
}
check('empty-profile',()=>{const c=fixture('empty-profile');c.workload.env.ACCEPTANCE_NO_FLUSH='1';through(c,'instrument');rejected(()=>stage(c,'train'),'empty_profile');assert.equal(fs.existsSync(path.join(c.run_dir,'artifacts/P')),false);});
check('wrong-shared-library',()=>{const c=fixture('wrong-shared-library',true);c.training[0].argv[0]='{build}/rogue';through(c,'instrument');rejected(()=>stage(c,'train'),'shared_identity_unproven');});
check('data-changed',()=>{const c=fixture('data-changed');stage(c,'init');fs.appendFileSync(c.datasets[0].path,'42\n');rejected(()=>stage(c,'baseline'),'dataset_changed');});
check('profile-tampered',()=>{const c=fixture('profile-tampered');through(c,'train');fs.writeFileSync(path.join(c.run_dir,'profile-live/foreign.gcda'),'bad');rejected(()=>stage(c,'check-profile'),'profile_changed');});
check('stage-order',()=>{const c=fixture('stage-order');stage(c,'init');rejected(()=>stage(c,'optimize'),'stage_order');});
const failed=results.filter(r=>r.status==='failed').length,skipped=results.filter(r=>r.status==='skipped').length;
const summary={status:failed?'failed':skipped?'incomplete':'passed',platform:process.platform,arch:process.arch,passed:results.length-failed-skipped,failed,skipped,results};
writeJSON(path.join(output,'summary.json'),summary);
console.log(JSON.stringify({...summary,output},null,2));
process.exitCode=failed?1:skipped?2:0;
