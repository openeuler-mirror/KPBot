#!/usr/bin/env node
'use strict';

/**
 * DynamicWorkflowManager — Claude Code Dynamic Workflows 状态机
 *
 * 管理 current_workflow_state 的完整生命周期：
 *   - 初始化/加载/持久化
 *   - 门控路由（gate routing）
 *   - workflow_trace 追加与校验
 *   - 候选/coverage skill 列表管理
 *   - 报告前合规自检
 *
 * 用法：
 *   作为模块:
 *     const { DynamicWorkflowManager } = require('./dynamic_workflow_manager.js');
 *     const dwm = new DynamicWorkflowManager('run-20260624-120000', './output');
 *
 *   CLI:
 *     node dynamic_workflow_manager.js init --run-id run-20260624-120000 --output-dir ./output
 *     node dynamic_workflow_manager.js gate-enter --state state.json --gate scenario-intake
 *     node dynamic_workflow_manager.js gate-complete --state state.json --gate scenario-intake --evidence ./evidence/
 *     node dynamic_workflow_manager.js validate --state state.json
 *     node dynamic_workflow_manager.js summary --state state.json
 */

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

/** 完整的门控序列（按架构图自顶向下） */
const GATE_SEQUENCE = [
  'bootstrap',
  'scenario-intake',
  'environment-backup',
  'environment-diagnosis',
  'service-health-check',
  'baseline',
  'evidence-collection',
  'candidate-routing',
  'candidate-skill-iteration',
  'coverage-skill-iteration',
  'report',
  'review-restore-archive',
];

/** 上游硬门控：失败时必须 block 整个流程 */
const HARD_GATES = new Set([
  'scenario-intake',
  'environment-backup',
  'environment-diagnosis',
  'service-health-check',
  'baseline',
]);

/** 主优化 skill 全集 */
const ALL_PRIMARY_SKILLS = [
  'application-config-optimization',
  'performance-library-selection',
  'cpu-affinity-optimization',
  'network-optimization',
  'compiler-optimization',
  'os-optimization',
  'bios-optimization',
  'accelerator-optimization',
  'hardware-upgrade-analysis',
  'other-optimization',
];

/** 合法 evidence_status 值 */
const EVIDENCE_STATUSES = new Set(['current', 'missing', 'stale', 'mixed', 'invalid']);

/**
 * 解析候选条目中的终态（completed/stopped/blocked），供 setCandidateSkillList 保留终态。
 * @param {string} subskillName
 * @param {Object} candidate  候选条目（可能带 status 字段）
 * @returns {string}
 */
function getCandidateStatus(subskillName, candidate) {
  const TERMINAL_STATUSES = ['completed', 'stopped', 'blocked'];
  if (candidate && TERMINAL_STATUSES.includes(candidate.status)) {
    return candidate.status;
  }
  const PRIMARY = [
    'application-config-optimization',
    'performance-library-selection',
    'cpu-affinity-optimization',
    'network-optimization',
    'compiler-optimization',
    'os-optimization',
    'bios-optimization',
    'accelerator-optimization',
    'hardware-upgrade-analysis',
    'other-optimization',
  ];
  return PRIMARY.includes(subskillName) ? 'pending' : 'pending';
}

/**
 * 根据日志条目推断 subagent 类型（未显式声明时）。
 * phase=implementation/execution/executor → executor；否则 analyzer。
 * @param {string|undefined} phase
 * @returns {string}
 */
function inferSubagentType(phase) {
  if (!phase) return 'analyzer';
  const implPhases = ['implementation', 'execution', 'executor', 'validation', 'apply'];
  return implPhases.includes(phase) ? 'executor' : 'analyzer';
}

// ---------------------------------------------------------------------------
// DynamicWorkflowManager
// ---------------------------------------------------------------------------

class DynamicWorkflowManager {
  /**
   * @param {string} runId       本轮运行 ID，如 run-20260624-120000
   * @param {string} outputDir   输出目录（状态文件将写入 <outputDir>/workflow_state.json）
   */
  constructor(runId, outputDir) {
    if (!runId || typeof runId !== 'string') {
      throw new Error('DynamicWorkflowManager: runId is required');
    }
    this.runId = runId;
    this.outputDir = outputDir || '.';
    this.statePath = path.join(this.outputDir, 'workflow_state.json');
    this.state = null;
  }

  // -----------------------------------------------------------------------
  // 初始化
  // -----------------------------------------------------------------------

  /**
   * 创建初始 workflow_state。必须在启动门控时调用。
   * @returns {object} 初始 state
   */
  initWorkflowState() {
    this.state = {
      current_gate: 'bootstrap',
      completed_gates: [],
      blocked_gate: null,
      next_gate: 'scenario-intake',
      current_run_id: this.runId,
      current_run_started_at: new Date().toISOString(),
      evidence_status: 'missing',
      candidate_skill_list: [],
      coverage_skill_list: [],
      active_workflow: null,
      workflow_trace: [],
      _version: '1.0',
      _created_at: new Date().toISOString(),
      _updated_at: new Date().toISOString(),
    };
    this._appendTrace('bootstrap', 'initialized', {
      run_id: this.runId,
      gate_sequence: GATE_SEQUENCE,
    });
    return this.state;
  }

  /**
   * 从已有 JSON 文件加载 state。
   * @param {string} filePath  state JSON 路径
   * @returns {object} 加载的 state
   */
  static load(filePath) {
    const raw = fs.readFileSync(filePath, 'utf-8');
    const state = JSON.parse(raw);
    DynamicWorkflowManager._validateState(state);
    return state;
  }

  /**
   * 加载已有 state 到当前实例。
   */
  loadState(filePath) {
    this.state = DynamicWorkflowManager.load(filePath);
    this.statePath = filePath;
    this.runId = this.state.current_run_id;
    return this.state;
  }

  // -----------------------------------------------------------------------
  // 门控管理
  // -----------------------------------------------------------------------

  /**
   * 进入新门控。自动更新 current_gate / next_gate。
   * @param {string} gateName  门控名称
   * @param {object} [meta]    附加元数据
   * @returns {object} 更新后的 state
   */
  enterGate(gateName, meta = {}) {
    this._ensureState();
    this._validateGate(gateName);

    if (this.state.blocked_gate) {
      throw new Error(
        `Cannot enter gate "${gateName}": blocked at "${this.state.blocked_gate}"`
      );
    }

    const idx = GATE_SEQUENCE.indexOf(gateName);
    this.state.current_gate = gateName;
    this.state.next_gate = idx >= 0 && idx < GATE_SEQUENCE.length - 1
      ? GATE_SEQUENCE[idx + 1]
      : null;

    this._appendTrace(gateName, 'entered', meta);
    this._touch();
    return this.state;
  }

  /**
   * 完成当前门控。
   * @param {string} gateName     门控名称
   * @param {object} [result]     门控结果 { evidence_path, status, ... }
   * @returns {object} 更新后的 state
   */
  completeGate(gateName, result = {}) {
    this._ensureState();
    this._validateGate(gateName);

    if (!this.state.completed_gates.includes(gateName)) {
      this.state.completed_gates.push(gateName);
    }
    this.state.current_gate = gateName;

    this._appendTrace(gateName, 'completed', {
      status: result.status || 'completed',
      evidence_path: result.evidence_path || null,
      ...result,
    });
    this._touch();
    return this.state;
  }

  /**
   * 阻塞当前门控。
   * @param {string} gateName  门控名称
   * @param {string} reason    阻塞原因
   * @param {object} [detail]  详细证据
   * @returns {object} 更新后的 state
   */
  blockGate(gateName, reason, detail = {}) {
    this._ensureState();
    this._validateGate(gateName);

    this.state.blocked_gate = gateName;
    this._appendTrace(gateName, 'blocked', { reason, ...detail });
    this._touch();
    return this.state;
  }

  /**
   * 解除阻塞。
   */
  unblock() {
    this._ensureState();
    const was = this.state.blocked_gate;
    this.state.blocked_gate = null;
    if (was) {
      this._appendTrace(was, 'unblocked', {});
    }
    this._touch();
    return this.state;
  }

  // -----------------------------------------------------------------------
  // 证据状态
  // -----------------------------------------------------------------------

  /**
   * 设置证据新鲜度状态。
   */
  setEvidenceStatus(status) {
    this._ensureState();
    if (!EVIDENCE_STATUSES.has(status)) {
      throw new Error(`Invalid evidence_status: "${status}". Must be one of: ${[...EVIDENCE_STATUSES].join(', ')}`);
    }
    this.state.evidence_status = status;
    this._appendTrace(this.state.current_gate, 'evidence-status-updated', { status });
    this._touch();
    return this.state;
  }

  // -----------------------------------------------------------------------
  // 候选 Skill 列表
  // -----------------------------------------------------------------------

  /**
   * 设置候选 skill 列表（evidence_candidate 阶段）。
   *
   * cpu-affinity-optimization 始终自动作为第一优先级加入候选列表。
   * 如果用户提供的 candidates 中已包含它，则移到首位并设为 highest；
   * 如果未包含，则自动预置一个 mandatory_baseline_check 条目。
   *
   * @param {Array<{subskill_name: string, priority: string, source_signal: string, reason: string}>} candidates
   */
  setCandidateSkillList(candidates) {
    this._ensureState();

    // 规范化字段名：支持 skill / subskill_name / name 三种 key
    candidates = candidates.map(c => ({
      subskill_name: c.subskill_name || c.skill || c.name || '',
      priority: c.priority || 'medium',
      source_signal: c.source_signal || '',
      reason: c.reason || '',
      phase: c.phase || 'evidence_candidate',
    })).filter(c => c.subskill_name);

    const MANDATORY_SKILL = 'cpu-affinity-optimization';
    const mandatoryReason = 'CPU 亲和性是所有服务器应用的基础优化，必须作为基线检查';

    // 分离出 cpu-affinity-optimization（如果用户提供了）
    const hasMandatory = candidates.find(c => c.subskill_name === MANDATORY_SKILL);
    const others = candidates.filter(c => c.subskill_name !== MANDATORY_SKILL);

    // 其余候选：performance-library-selection 必须排在 os-optimization 之前
    // （库替换影响 malloc 路径，是 OS 大页/GLIBC_TUNABLES 调优的前置依赖）。
    const PERF_LIB = 'performance-library-selection';
    const OS_OPT = 'os-optimization';
    const hasPerfLib = others.some(c => c.subskill_name === PERF_LIB);
    const hasOsOpt = others.some(c => c.subskill_name === OS_OPT);
    if (hasPerfLib && hasOsOpt) {
      const perfIdx = others.findIndex(c => c.subskill_name === PERF_LIB);
      const osIdx = others.findIndex(c => c.subskill_name === OS_OPT);
      if (perfIdx > osIdx) {
        const [perfItem] = others.splice(perfIdx, 1);
        others.splice(osIdx, 0, perfItem);
      }
    }

    // compiler-optimization 必须排在候选列表最末位
    // （编译级改造会重建二进制、干扰其他 skill 的 A/B 收益归因，
    //  需等其他运行时类手段验证完毕后再执行）。
    const COMPILER_OPT = 'compiler-optimization';
    const compilerIdx = others.findIndex(c => c.subskill_name === COMPILER_OPT);
    if (compilerIdx !== -1 && compilerIdx !== others.length - 1) {
      const [compilerItem] = others.splice(compilerIdx, 1);
      others.push(compilerItem);
    }

    const list = [];

    // 从旧列表中保留已完成/已停止/已阻塞 skill 的终态，防止 set-candidates 覆盖回滚
    const TERMINAL_STATUSES = ['completed', 'stopped', 'blocked'];
    const previousList = this.state.candidate_skill_list || [];
    const prevTerminal = new Map(
      previousList.map(c => [c.subskill_name, c])
    );

    // 始终在第一位置入 cpu-affinity-optimization
    list.push({
      candidate_id: 'candidate-skill-001',
      phase: 'evidence_candidate',
      subskill_name: MANDATORY_SKILL,
      priority: 'highest',
      reason: hasMandatory ? (hasMandatory.reason || mandatoryReason) : mandatoryReason,
      source_signal: hasMandatory
        ? (hasMandatory.source_signal || 'mandatory_baseline_check')
        : 'mandatory_baseline_check',
      status: prevTerminal.has(MANDATORY_SKILL) && TERMINAL_STATUSES.includes(prevTerminal.get(MANDATORY_SKILL).status)
        ? prevTerminal.get(MANDATORY_SKILL).status
        : getCandidateStatus(MANDATORY_SKILL, hasMandatory),
      result: prevTerminal.get(MANDATORY_SKILL) ? prevTerminal.get(MANDATORY_SKILL).result : undefined,
    });

    // 其余候选按用户提供的顺序排列
    for (let i = 0; i < others.length; i++) {
      const prev = prevTerminal.get(others[i].subskill_name);
      list.push({
        candidate_id: `candidate-skill-${String(i + 2).padStart(3, '0')}`,
        phase: 'evidence_candidate',
        subskill_name: others[i].subskill_name,
        priority: others[i].priority || 'medium',
        reason: others[i].reason || '',
        source_signal: others[i].source_signal || '',
        status: prev && TERMINAL_STATUSES.includes(prev.status)
          ? prev.status
          : getCandidateStatus(others[i].subskill_name, others[i]),
        result: prev ? prev.result : undefined,
      });
    }

    this.state.candidate_skill_list = list;
    this._appendTrace('candidate-routing', 'candidate-list-generated', {
      count: list.length,
      mandatory_first: MANDATORY_SKILL,
      skills: list.map(c => c.subskill_name),
    });
    this._touch();
    return this.state;
  }

  /**
   * 追加额外证据候选 skill（添加到末尾，不会覆盖 mandatory 第一位置）。
   */
  appendCandidateSkill(subskillName, opts = {}) {
    this._ensureState();
    const lastIdx = this.state.candidate_skill_list.length;
    this.state.candidate_skill_list.push({
      candidate_id: `candidate-skill-${String(lastIdx + 1).padStart(3, '0')}`,
      phase: 'evidence_candidate',
      subskill_name: subskillName,
      priority: opts.priority || 'medium',
      reason: opts.reason || '',
      source_signal: opts.source_signal || '',
      status: 'pending',
    });
    this._touch();
    return this.state;
  }

  /**
   * 从候选列表中标记 skill 状态。
   */
  updateCandidateStatus(subskillName, status, result = {}) {
    this._ensureState();
    const entry = this.state.candidate_skill_list.find(
      c => c.subskill_name === subskillName
    );
    if (!entry) {
      throw new Error(`Candidate skill "${subskillName}" not found in candidate_skill_list`);
    }
    entry.status = status;
    entry.result = result;
    this._touch();
    return this.state;
  }

  /**
   * 添加 coverage skill（候选完成后，未命中的主优化 skill）。
   */
  addCoverageSkill(subskillName) {
    this._ensureState();
    const exists = this.state.coverage_skill_list.find(
      c => c.subskill_name === subskillName
    );
    if (!exists) {
      this.state.coverage_skill_list.push({
        phase: 'coverage',
        subskill_name: subskillName,
        status: 'pending',
      });
    }
    this._touch();
    return this.state;
  }

  /**
   * 自动生成 coverage skill 列表（未被 candidate 列表覆盖的主优化 skill）。
   */
  autoGenerateCoverageList() {
    this._ensureState();
    const candidateNames = new Set(
      this.state.candidate_skill_list.map(c => c.subskill_name)
    );
    for (const skill of ALL_PRIMARY_SKILLS) {
      if (!candidateNames.has(skill)) {
        this.addCoverageSkill(skill);
      }
    }
    this._appendTrace('coverage-skill-iteration', 'coverage-list-generated', {
      count: this.state.coverage_skill_list.length,
      skills: this.state.coverage_skill_list.map(c => c.subskill_name),
    });
    this._touch();
    return this.state;
  }

  // -----------------------------------------------------------------------
  // Workflow Trace
  // -----------------------------------------------------------------------

  /**
   * 追加 workflow_trace 条目。
   * @param {string} gate      门控名称
   * @param {string} event     事件：entered|completed|blocked|skipped|unblocked|...
   * @param {object} [detail]  附加信息
   */
  appendTraceEntry(gate, event, detail = {}) {
    this._ensureState();
    this._appendTrace(gate, event, detail);
    this._touch();
    return this.state;
  }

  // -----------------------------------------------------------------------
  // 合规自检
  // -----------------------------------------------------------------------

  /**
   * 执行 Dynamic Workflows 合规自检。报告生成前必须调用。
   * @returns {{ passed: boolean, checks: Array, score: number, summary: string }}
   */
  validateCompliance() {
    this._ensureState();
    const checks = [];
    let failures = 0;
    let warnings = 0;

    // 1. current_workflow_state 非空
    if (!this.state) {
      return { passed: false, checks: [{ item: 'workflow_state_exists', result: 'FAIL', detail: 'state is null' }], score: 0, summary: 'state 未初始化' };
    }
    checks.push({ item: 'workflow_state_exists', result: 'PASS' });

    // 2. workflow_trace 条目数 ≥ 完成的门控数
    const traceCount = this.state.workflow_trace.length;
    const completedCount = this.state.completed_gates.length;
    if (traceCount >= completedCount) {
      checks.push({ item: 'workflow_trace_coverage', result: 'PASS', detail: `${traceCount} trace entries for ${completedCount} completed gates` });
    } else {
      checks.push({ item: 'workflow_trace_coverage', result: 'FAIL', detail: `only ${traceCount} trace entries for ${completedCount} completed gates` });
      failures++;
    }

    // 3. runId 非空
    if (this.state.current_run_id) {
      checks.push({ item: 'run_id_present', result: 'PASS', detail: this.state.current_run_id });
    } else {
      checks.push({ item: 'run_id_present', result: 'FAIL', detail: 'missing' });
      failures++;
    }

    // 4. 至少有一个候选或 coverage skill
    const totalSkills = this.state.candidate_skill_list.length + this.state.coverage_skill_list.length;
    if (totalSkills > 0) {
      checks.push({ item: 'skill_coverage', result: 'PASS', detail: `${this.state.candidate_skill_list.length} candidate + ${this.state.coverage_skill_list.length} coverage skills` });
    } else {
      checks.push({ item: 'skill_coverage', result: 'WARN', detail: 'no skills in candidate or coverage lists (may be valid if still in early gates)' });
      warnings++;
    }

    // 5. 检查是否跳过上游硬门控（completed_gates 应按顺序）
    const completedSet = new Set(this.state.completed_gates);
    let foundMissing = false;
    for (const hard of HARD_GATES) {
      if (!completedSet.has(hard) && this.state.current_gate !== hard) {
        // 仅当 current_gate 在 hard 之后时才检查
        const hardIdx = GATE_SEQUENCE.indexOf(hard);
        const curIdx = GATE_SEQUENCE.indexOf(this.state.current_gate);
        if (curIdx > hardIdx) {
          checks.push({ item: `hard_gate_${hard}`, result: 'FAIL', detail: `gate "${hard}" was skipped but current_gate is "${this.state.current_gate}"` });
          failures++;
          foundMissing = true;
        }
      }
    }
    if (!foundMissing) {
      checks.push({ item: 'hard_gate_order', result: 'PASS', detail: 'all upstream hard gates completed in order' });
    }

    // 6. evidence_status
    if (this.state.evidence_status && EVIDENCE_STATUSES.has(this.state.evidence_status)) {
      checks.push({ item: 'evidence_status_valid', result: 'PASS', detail: this.state.evidence_status });
    } else {
      checks.push({ item: 'evidence_status_valid', result: 'WARN', detail: `status is "${this.state.evidence_status}"` });
      warnings++;
    }

    // 7. blocked_gate 与 current_gate 一致
    if (this.state.blocked_gate) {
      checks.push({ item: 'blocked_state_consistent', result: 'WARN', detail: `blocked at "${this.state.blocked_gate}", current is "${this.state.current_gate}"` });
      warnings++;
    } else {
      checks.push({ item: 'blocked_state_consistent', result: 'PASS', detail: 'not blocked' });
    }

    // 8. 若已完成 report gate，必须检查耗时字段
    if (this.state.completed_gates.includes('report')) {
      const hasTimingSummary = !!(this.state.agent_timing_summary);
      const hasOptimizationTiming = !!(this.state.optimization_timing);
      const hasTimingDetails = !!(this.state.optimization_timing_details);

      if (hasTimingSummary && hasOptimizationTiming && hasTimingDetails) {
        checks.push({ item: 'report_timing_fields', result: 'PASS', detail: 'agent_timing_summary, optimization_timing, optimization_timing_details all present' });
      } else {
        const missing = [];
        if (!hasTimingSummary) missing.push('agent_timing_summary');
        if (!hasOptimizationTiming) missing.push('optimization_timing');
        if (!hasTimingDetails) missing.push('optimization_timing_details');
        checks.push({ item: 'report_timing_fields', result: 'FAIL', detail: `missing: ${missing.join(', ')}` });
        failures++;
      }

      // 8b. 若已完成 report gate，必须检查 execution_log
      const hasExecLog = !!(this.state.execution_log) && Array.isArray(this.state.execution_log) && this.state.execution_log.length > 0;
      if (hasExecLog) {
        const appliedCount = this.state.execution_log.filter(e => e.status === 'applied').length;
        const reversibleCount = this.state.execution_log.filter(e => e.status === 'applied' && e.reverse_cmd).length;
        checks.push({ item: 'execution_log_present', result: 'PASS', detail: `${appliedCount} applied steps, ${reversibleCount} reversible` });
      } else {
        checks.push({ item: 'execution_log_present', result: 'WARN', detail: 'execution_log 为空，无法生成恢复计划' });
        warnings++;
      }
    }

    // 9. 若已进入迭代阶段，检查 cpu-affinity first
    const candidateIterComplete = this.state.completed_gates.includes('candidate-skill-iteration');
    const coverageIterComplete = this.state.completed_gates.includes('coverage-skill-iteration');
    if (candidateIterComplete || coverageIterComplete) {
      const cpuFirst = this.validateCpuAffinityFirst();
      checks.push({
        item: 'cpu_affinity_first',
        result: cpuFirst.passed ? 'PASS' : 'FAIL',
        detail: cpuFirst.passed
          ? 'cpu-affinity-optimization executed first and completed before other skills'
          : `cpu-affinity validation failed: ${cpuFirst.checks.filter(c => c.result === 'FAIL').map(c => c.item).join(', ')}`,
      });
      if (!cpuFirst.passed) failures++;
    }

    // 10. 若已完成所有 skill 迭代，检查 per_skill_gain_summary
    if (this.state.per_skill_gain_summary && Array.isArray(this.state.per_skill_gain_summary)) {
      const totalSkills = this.state.candidate_skill_list.length + this.state.coverage_skill_list.length;
      if (this.state.per_skill_gain_summary.length >= totalSkills) {
        checks.push({ item: 'per_skill_gain_summary_coverage', result: 'PASS', detail: `${this.state.per_skill_gain_summary.length} skills with gain attribution` });
      } else {
        checks.push({ item: 'per_skill_gain_summary_coverage', result: 'WARN', detail: `${this.state.per_skill_gain_summary.length} of ${totalSkills} skills have gain attribution` });
        warnings++;
      }
    }

    const total = checks.length;
    const passed = checks.filter(c => c.result === 'PASS').length;
    const score = total > 0 ? Math.round((passed / total) * 100) : 0;

    const result = {
      passed: failures === 0,
      score,
      failures,
      warnings,
      checks,
      summary: failures === 0
        ? `All ${total} checks passed (${warnings} warnings)`
        : `${failures}/${total} checks failed, ${warnings} warnings`,
    };

    this._appendTrace(this.state.current_gate, 'compliance-check', result);
    this._touch();
    return result;
  }

  /**
   * 报告输入校验：报告生成前必须调用，验证所有必填字段非空。
   * 若校验失败，报告的 overall_progress.status 必须为 blocked/degraded。
   * @returns {{ passed: boolean, issues: string[], checks: Array }}
   */
  validateReportInputs() {
    this._ensureState();
    const issues = [];
    const checks = [];

    // 1. agent_timing_summary
    if (this.state.agent_timing_summary && typeof this.state.agent_timing_summary === 'object' && Object.keys(this.state.agent_timing_summary).length > 0) {
      checks.push({ item: 'agent_timing_summary', result: 'PASS' });
    } else {
      checks.push({ item: 'agent_timing_summary', result: 'FAIL', detail: '必填 — 至少包含 total_seconds 和 per_gate 汇总' });
      issues.push('agent_timing_summary 缺失或为空');
    }

    // 2. optimization_timing
    if (this.state.optimization_timing && typeof this.state.optimization_timing === 'object' && Object.keys(this.state.optimization_timing).length > 0) {
      checks.push({ item: 'optimization_timing', result: 'PASS' });
    } else {
      checks.push({ item: 'optimization_timing', result: 'FAIL', detail: '必填 — 至少包含 total_analysis_seconds、total_implementation_seconds、total_validation_seconds' });
      issues.push('optimization_timing 缺失或为空');
    }

    // 3. optimization_timing_details
    if (this.state.optimization_timing_details && Array.isArray(this.state.optimization_timing_details) && this.state.optimization_timing_details.length > 0) {
      checks.push({ item: 'optimization_timing_details', result: 'PASS', detail: `${this.state.optimization_timing_details.length} records` });
    } else {
      checks.push({ item: 'optimization_timing_details', result: 'FAIL', detail: '必填 — 每个 skill 和每轮动作的耗时记录数组' });
      issues.push('optimization_timing_details 缺失或为空');
    }

    // 4. subagent_invocation_log
    if (this.state.subagent_invocation_log && Array.isArray(this.state.subagent_invocation_log) && this.state.subagent_invocation_log.length > 0) {
      checks.push({ item: 'subagent_invocation_log', result: 'PASS', detail: `${this.state.subagent_invocation_log.length} invocations` });
    } else {
      checks.push({ item: 'subagent_invocation_log', result: 'FAIL', detail: '必填 — Claude Code 平台不可降级，每个 skill 必须由独立 subagent 执行' });
      issues.push('subagent_invocation_log 缺失或为空（subagent 模式未启用？）');
    }

    // 5. per_skill_gain_summary（WARN 级别 — 每个已完成 skill 应有独立收益归因）
    if (this.state.per_skill_gain_summary && Array.isArray(this.state.per_skill_gain_summary) && this.state.per_skill_gain_summary.length > 0) {
      checks.push({ item: 'per_skill_gain_summary', result: 'PASS', detail: `${this.state.per_skill_gain_summary.length} skills` });
    } else {
      checks.push({ item: 'per_skill_gain_summary', result: 'WARN', detail: '建议 — 每个已完成 skill 应有独立收益归因记录' });
      issues.push('per_skill_gain_summary 缺失或为空（无法验证单 skill 收益归因）');
    }

    const passed = issues.length === 0;
    this._appendTrace(this.state.current_gate, 'report-input-validation', { passed, issues, checks });
    this._touch();
    return { passed, issues, checks };
  }

  /**
   * 验证 cpu-affinity-optimization 是否在所有其他 skill 之前执行完成。
   * 报告生成前必须调用。
   * @returns {{ passed: boolean, checks: Array, cpu_affinity_first_verified: boolean, cpu_affinity_completed_before_next: boolean }}
   */
  validateCpuAffinityFirst() {
    this._ensureState();
    const checks = [];
    const MANDATORY_SKILL = 'cpu-affinity-optimization';

    const candidateList = this.state.candidate_skill_list || [];
    const cpuIdx = candidateList.findIndex(c => c.subskill_name === MANDATORY_SKILL);
    const cpuEntry = cpuIdx >= 0 ? candidateList[cpuIdx] : null;

    // Check 1: cpu-affinity is in candidate list
    if (cpuEntry) {
      checks.push({ item: 'cpu_affinity_in_candidate_list', result: 'PASS', detail: `position ${cpuIdx}` });
    } else {
      checks.push({ item: 'cpu_affinity_in_candidate_list', result: 'FAIL', detail: 'cpu-affinity-optimization not found in candidate_skill_list' });
    }

    // Check 2: cpu-affinity is first in candidate list
    if (cpuIdx === 0) {
      checks.push({ item: 'cpu_affinity_first_position', result: 'PASS' });
    } else {
      checks.push({ item: 'cpu_affinity_first_position', result: 'FAIL', detail: `found at position ${cpuIdx}, expected 0` });
    }

    // Check 3: cpu-affinity is completed or stopped
    const perSkillState = this.state.per_skill_iteration_state || {};
    const cpuState = perSkillState[MANDATORY_SKILL] || {};
    const cpuStatus = cpuState.status || (cpuEntry ? cpuEntry.status : 'pending');
    const cpuCompleted = cpuStatus === 'completed' || cpuStatus === 'stopped';

    if (cpuCompleted) {
      checks.push({ item: 'cpu_affinity_completed', result: 'PASS', detail: `status=${cpuStatus}` });
    } else {
      checks.push({ item: 'cpu_affinity_completed', result: 'FAIL', detail: `status=${cpuStatus}, expected completed or stopped` });
    }

    // Check 4: No other skill executed before cpu-affinity completion
    const otherSkillsStarted = this._otherSkillsStartedBeforeCpuAffinity(perSkillState);
    if (!otherSkillsStarted) {
      checks.push({ item: 'no_other_skill_before_cpu_affinity', result: 'PASS' });
    } else {
      checks.push({ item: 'no_other_skill_before_cpu_affinity', result: 'FAIL', detail: 'other skills started before cpu-affinity completed' });
    }

    const failures = checks.filter(c => c.result === 'FAIL').length;
    const cpu_affinity_first_verified = cpuCompleted && cpuIdx === 0;
    const cpu_affinity_completed_before_next = cpuCompleted && !otherSkillsStarted;

    return {
      passed: failures === 0,
      checks,
      cpu_affinity_first_verified,
      cpu_affinity_completed_before_next,
    };
  }

  /**
   * 启发式检测：是否有其他 skill 在 cpu-affinity 完成前开始执行。
   * 基于 per_skill_iteration_state 中的 round_gains_pct 条目数推断。
   * @param {Object} perSkillState
   * @returns {boolean}
   */
  _otherSkillsStartedBeforeCpuAffinity(perSkillState) {
    const cpuState = perSkillState['cpu-affinity-optimization'] || {};
    const cpuRounds = (cpuState.round_gains_pct || []).length;
    const cpuStatus = cpuState.status;

    for (const [name, state] of Object.entries(perSkillState)) {
      if (name === 'cpu-affinity-optimization') continue;
      const otherRounds = (state.round_gains_pct || []).length;
      // 如果其他 skill 有轮次记录但 cpu-affinity 没有
      if (otherRounds > 0 && cpuRounds === 0) return true;
      // 如果 cpu 从未完成但其他 skill 有记录
      if (!['completed', 'stopped'].includes(cpuStatus) && otherRounds > 0) return true;
    }
    return false;
  }

  // -----------------------------------------------------------------------
  // 证据完整性 / Skill 完整性 / Subagent Prompt 预检
  // -----------------------------------------------------------------------

  /**
   * 验证证据快照的必填字段是否非空。
   * evidence-collection gate 完成时必须调用。任一必填字段为空时返回 passed=false，
   * 主 agent 必须阻塞在 evidence-collection 门控，补采后重新校验。
   *
   * 字段按 perf 能力分层：
   *   - perf record 依赖：hotspot_function_rank, hotspot_dso_rank
   *     perf record -e task-clock 采集调用栈/DSO，在 perf=degraded 时仍可用。
   *     仅当 perf=failed（perf 命令缺失或完全不可用）时才允许为空。
   *   - 硬件 PMU 依赖：topdown
   *     perf stat -e cycles,instructions 采集，需要硬件 PMU。
   *     perf=degraded（硬件 PMU 不可用）或 perf=failed 时允许为空。
   *   - 非 perf 依赖：threading
   *     mpstat/pidstat 采集，任何情况下都必须非空。
   *
   * 交叉校验：perf 状态来自 state.environment_diagnosis.perf_pmu_status 或
   * --perf-status 参数。如果 perf 状态为 passed/degraded（即 perf record 可用），
   * 但 hotspot_function_rank/hotspot_dso_rank 为空，则即使 --allow-degraded 也
   * 拒绝通过——这是"误判后未补采"的场景，agent 必须回头用 perf record 补采。
   *
   * @param {string} [summaryPath]  performance_signal_summary.json 路径
   * @param {boolean} [allowDegraded]  是否允许 perf 不可用时的降级模式
   * @param {string} [perfStatus]  perf 状态 (passed/degraded/failed)；不传则从 state.environment_diagnosis.perf_pmu_status 读取
   * @returns {{ passed: boolean, degraded: boolean, checks: Array, missing_fields: string[], degraded_reason: string|null, perf_status: string }}
   */
  verifyEvidenceCompleteness(summaryPath, allowDegraded = false, perfStatus = null) {
    this._ensureState();
    const checks = [];
    const missing_fields = [];

    let summary = null;
    if (summaryPath) {
      try {
        summary = JSON.parse(fs.readFileSync(summaryPath, 'utf-8'));
      } catch (e) {
        const result = { passed: false, degraded: false, checks: [{ item: 'summary_file_readable', result: 'FAIL', detail: e.message }], missing_fields: ['__file_unreadable__'], degraded_reason: null, perf_status: 'unknown' };
        this._appendTrace(this.state.current_gate, 'evidence-completeness-check', result);
        this._touch();
        return result;
      }
    } else if (this.state.evidence_summary) {
      summary = this.state.evidence_summary;
    } else if (this.state.performance_signal_summary) {
      summary = this.state.performance_signal_summary;
    }

    if (!summary) {
      const result = { passed: false, degraded: false, checks: [{ item: 'summary_present', result: 'FAIL', detail: 'performance_signal_summary not provided and not found in state' }], missing_fields: ['__summary_missing__'], degraded_reason: null, perf_status: 'unknown' };
      this._appendTrace(this.state.current_gate, 'evidence-completeness-check', result);
      this._touch();
      return result;
    }
    checks.push({ item: 'summary_present', result: 'PASS' });

    // 确定 perf 状态
    let perf = perfStatus;
    if (!perf) {
      const diag = this.state.environment_diagnosis || {};
      perf = diag.perf_pmu_status || diag.perf_status || 'unknown';
    }
    checks.push({ item: 'perf_status', result: 'PASS', detail: `perf_pmu_status=${perf}` });

    const isEmpty = (val, type) => type === 'array'
      ? (!Array.isArray(val) || val.length === 0)
      : (!val || typeof val !== 'object' || Object.keys(val).length === 0);

    // perf record 依赖字段：perf=passed 或 perf=degraded 时必须非空（perf record 可用）
    // 仅 perf=failed 时允许为空
    const PERF_RECORD_FIELDS = [
      { key: 'hotspot_function_rank', type: 'array', label: '热点函数排序' },
      { key: 'hotspot_dso_rank',      type: 'array', label: '热点 DSO 排序' },
    ];

    // 硬件 PMU 依赖字段：perf=passed 时必须非空，perf=degraded 或 failed 时允许为空
    const PMU_FIELDS = [
      { key: 'topdown', type: 'object', label: 'topdown 指标' },
    ];

    // 非 perf 依赖字段：任何情况下都必须非空
    const NON_PERF_FIELDS = [
      { key: 'threading', type: 'object', label: '线程分布' },
    ];

    const perfRecordUsable = (perf === 'passed' || perf === 'degraded');
    const pmuUsable = (perf === 'passed');

    // 检查 perf record 依赖字段
    for (const field of PERF_RECORD_FIELDS) {
      const val = summary[field.key];
      if (isEmpty(val, field.type)) {
        if (perfRecordUsable) {
          // perf record 可用但字段为空 → 必须补采，不允许降级
          checks.push({ item: `field_${field.key}`, result: 'FAIL', detail: `${field.label} (${field.key}) 为空 — perf 状态为 ${perf}，perf record 可用，必须用 perf record 补采` });
          missing_fields.push(field.key);
        } else if (allowDegraded) {
          // perf=failed，降级模式放行
          checks.push({ item: `field_${field.key}`, result: 'WARN', detail: `${field.label} (${field.key}) 为空 — perf=${perf}，降级模式放行` });
        } else {
          checks.push({ item: `field_${field.key}`, result: 'FAIL', detail: `${field.label} (${field.key}) 为空 — perf=${perf}，如确属不可用请用 --allow-degraded` });
          missing_fields.push(field.key);
        }
      } else {
        checks.push({ item: `field_${field.key}`, result: 'PASS', detail: `${field.label}: ${val.length} 条` });
      }
    }

    // 检查硬件 PMU 依赖字段
    for (const field of PMU_FIELDS) {
      const val = summary[field.key];
      if (isEmpty(val, field.type)) {
        if (pmuUsable) {
          checks.push({ item: `field_${field.key}`, result: 'FAIL', detail: `${field.label} (${field.key}) 为空 — perf=passed，硬件 PMU 可用，必须采集` });
          missing_fields.push(field.key);
        } else if (allowDegraded) {
          checks.push({ item: `field_${field.key}`, result: 'WARN', detail: `${field.label} (${field.key}) 为空 — perf=${perf}，硬件 PMU 不可用，降级模式放行` });
        } else {
          checks.push({ item: `field_${field.key}`, result: 'FAIL', detail: `${field.label} (${field.key}) 为空 — perf=${perf}，如硬件 PMU 确不可用请用 --allow-degraded` });
          missing_fields.push(field.key);
        }
      } else {
        checks.push({ item: `field_${field.key}`, result: 'PASS', detail: `${field.label}: ${Object.keys(val).length} 键` });
      }
    }

    // 检查非 perf 依赖字段
    for (const field of NON_PERF_FIELDS) {
      const val = summary[field.key];
      if (isEmpty(val, field.type)) {
        checks.push({ item: `field_${field.key}`, result: 'FAIL', detail: `${field.label} (${field.key}) 为空 — 非 perf 依赖字段，任何情况下都必须非空` });
        missing_fields.push(field.key);
      } else {
        checks.push({ item: `field_${field.key}`, result: 'PASS', detail: `${field.label}: ${Object.keys(val).length} 键` });
      }
    }

    // 降级模式额外校验：有字段被降级放行时，必须提供 degraded_reason
    const hasWarns = checks.some(c => c.result === 'WARN');
    let degraded = false;
    let degraded_reason = null;
    if (allowDegraded && hasWarns) {
      degraded_reason = summary.degraded_reason || summary.perf_degraded_reason || '';
      if (!degraded_reason) {
        checks.push({ item: 'degraded_reason', result: 'FAIL', detail: '降级模式下有字段为空，但 summary 中未提供 degraded_reason 说明原因' });
        missing_fields.push('degraded_reason');
      } else {
        checks.push({ item: 'degraded_reason', result: 'PASS', detail: degraded_reason });
        degraded = true;
      }
    }

    const passed = missing_fields.length === 0;
    const result = { passed, degraded, checks, missing_fields, degraded_reason, perf_status: perf };
    this._appendTrace(this.state.current_gate, 'evidence-completeness-check', { passed, degraded, missing_count: missing_fields.length, perf_status: perf });
    this._touch();
    return result;
  }

  /**
   * 验证 cpu-affinity-optimization 已排在候选首位且状态为 completed/stopped。
   * 进入任何其他候选或 coverage skill 的分析/执行 subagent 前必须调用。
   * exit≠0 时主 agent 必须阻塞。
   *
   * @returns {{ passed: boolean, checks: Array }}
   */
  verifyOrder() {
    this._ensureState();
    const result = this.validateCpuAffinityFirst();
    this._appendTrace(this.state.current_gate, 'verify-order', { passed: result.passed });
    this._touch();
    return result;
  }

  /**
   * 验证单个 skill 的全部 candidate_actions 是否都有结论。
   * 任意 skill 标记为 completed 前必须调用。incomplete_skills 非空时禁止标记 completed。
   *
   * 校验来源：subagent_invocation_log 中该 skill 的分析 subagent 输出（result.candidate_actions），
   * 以及 per_skill_iteration_state 中该 skill 的 applied_action_ids / rejected_actions。
   *
   * "有结论"的定义：
   *   - action_id 出现在 applied_action_ids（已执行验证） 或
   *   - action_id 出现在 rejected_optimization_actions（已显式拒绝，附原因） 或
   *   - skill 状态为 stopped/blocked 且有 stop_reason/block_reason 覆盖全部未验证动作
   *
   * @param {string} [subskillName]  指定 skill；不传则校验所有已 completed 的 skill
   * @returns {{ passed: boolean, incomplete_skills: Array<{subskill, unresolved_actions}>, checks: Array }}
   */
  verifySkillCompleteness(subskillName) {
    this._ensureState();
    const checks = [];
    const incomplete_skills = [];

    const log = this.state.subagent_invocation_log || [];
    const iterState = this.state.per_skill_iteration_state || {};
    const candidateList = this.state.candidate_skill_list || [];
    const coverageList = this.state.coverage_skill_list || [];
    const allSkills = [...candidateList, ...coverageList];

    const skillsToCheck = subskillName
      ? allSkills.filter(s => s.subskill_name === subskillName)
      : allSkills.filter(s => s.status === 'completed');

    if (skillsToCheck.length === 0 && subskillName) {
      checks.push({ item: 'skill_found', result: 'FAIL', detail: `skill "${subskillName}" not found in candidate or coverage list` });
      return { passed: false, incomplete_skills: [{ subskill: subskillName, unresolved_actions: [] }], checks };
    }
    checks.push({ item: 'skill_found', result: 'PASS', detail: `${skillsToCheck.length} skill(s) to check` });

    for (const skillEntry of skillsToCheck) {
      const name = skillEntry.subskill_name;
      const skillStatus = skillEntry.status;

      const analysisLogs = log.filter(
        e => e.subskill === name && (e.phase === 'analysis' || !e.phase)
      );
      const analysisLog = analysisLogs.length > 0 ? analysisLogs[analysisLogs.length - 1] : null;
      const candidate_actions = analysisLog && analysisLog.result && analysisLog.result.candidate_actions
        ? analysisLog.result.candidate_actions
        : (analysisLog && analysisLog.candidate_actions ? analysisLog.candidate_actions : []);

      if (candidate_actions.length === 0) {
        if (skillStatus === 'stopped' || skillStatus === 'blocked') {
          const reason = (iterState[name] && (iterState[name].stop_reason || iterState[name].block_reason))
            || (skillEntry.result && (skillEntry.result.stop_reason || skillEntry.result.block_reason))
            || 'no candidate actions (skill stopped/blocked)';
          checks.push({ item: `skill_${name}_no_actions_stopped`, result: 'PASS', detail: reason });
        } else {
          checks.push({ item: `skill_${name}_candidate_actions`, result: 'FAIL', detail: 'no candidate_actions found in analysis subagent log' });
          incomplete_skills.push({ subskill: name, unresolved_actions: [], reason: 'no candidate_actions found' });
        }
        continue;
      }

      const applied = new Set(iterState[name] && iterState[name].applied_action_ids || []);
      const rejected = new Set(
        (iterState[name] && iterState[name].rejected_action_ids || [])
          .concat(analysisLog && analysisLog.result && analysisLog.result.rejected_optimization_actions
            ? analysisLog.result.rejected_optimization_actions.map(a => a.action_id || a)
            : [])
      );

      // ④ executor 实施门控：该 skill 有已实施动作时，必须存在独立 executor 的实施记录。
      //    主 agent 自行实施/验证（未启动执行验证 subagent）视为合规失败。
      const implLogs = log.filter(
        e => e.subskill === name && e.phase === 'implementation' && e.subagent_type === 'executor'
      );
      if (applied.size > 0 && implLogs.length === 0) {
        checks.push({
          item: `skill_${name}_executor_mandatory`,
          result: 'FAIL',
          detail: `${applied.size} applied actions but no executor implementation record found — main agent must not execute changes directly`,
        });
        incomplete_skills.push({
          subskill: name,
          unresolved_actions: [...applied],
          reason: 'executor_mandatory_violation',
        });
        continue;
      }
      if (applied.size > 0) {
        checks.push({
          item: `skill_${name}_executor_mandatory`,
          result: 'PASS',
          detail: `${implLogs.length} executor implementation record(s) for ${applied.size} applied actions`,
        });
      }

      const unresolved = [];
      for (const action of candidate_actions) {
        const aid = action.action_id || action.id || action;
        if (!applied.has(aid) && !rejected.has(aid)) {
          unresolved.push(aid);
        }
      }

      if (unresolved.length === 0) {
        checks.push({ item: `skill_${name}_completeness`, result: 'PASS', detail: `${candidate_actions.length} actions all resolved (${applied.size} applied, ${rejected.size} rejected)` });
      } else {
        if (skillStatus === 'stopped' || skillStatus === 'blocked') {
          const reason = (iterState[name] && (iterState[name].stop_reason || iterState[name].block_reason)) || 'skill stopped/blocked';
          checks.push({ item: `skill_${name}_completeness`, result: 'PASS', detail: `${unresolved.length} unresolved actions covered by stop/block reason: ${reason}` });
        } else {
          checks.push({ item: `skill_${name}_completeness`, result: 'FAIL', detail: `${unresolved.length} unresolved actions: ${unresolved.join(', ')}` });
          incomplete_skills.push({ subskill: name, unresolved_actions: unresolved });
        }
      }

      // steps_covered 覆盖校验：分析 subagent 输出的 steps_covered 必须覆盖 SKILL.md 定义的全部手段类别
      const stepsCovered = analysisLog && analysisLog.result && analysisLog.result.steps_covered
        ? analysisLog.result.steps_covered
        : (analysisLog && analysisLog.steps_covered ? analysisLog.steps_covered : null);

      if (stepsCovered && Array.isArray(stepsCovered) && stepsCovered.length > 0) {
        const coveredSet = new Set(stepsCovered.map(s => typeof s === 'string' ? s : (s.category || s.name || s)));
        const requiredCategories = this._getRequiredCategories(name);
        const uncovered = requiredCategories.filter(c => !coveredSet.has(c));

        if (uncovered.length === 0) {
          checks.push({ item: `skill_${name}_steps_covered`, result: 'PASS', detail: `${stepsCovered.length} steps covered, all ${requiredCategories.length} required categories present` });
        } else {
          if (skillStatus === 'stopped' || skillStatus === 'blocked') {
            const reason = (iterState[name] && (iterState[name].stop_reason || iterState[name].block_reason)) || 'skill stopped/blocked';
            checks.push({ item: `skill_${name}_steps_covered`, result: 'PASS', detail: `${uncovered.length} uncovered categories covered by stop/block reason: ${reason}` });
          } else {
            checks.push({ item: `skill_${name}_steps_covered`, result: 'FAIL', detail: `${uncovered.length} uncovered categories: ${uncovered.join(', ')}` });
            incomplete_skills.push({ subskill: name, unresolved_actions: [], uncovered_categories: uncovered });
          }
        }
      } else if (skillStatus === 'completed') {
        // completed skill 必须有 steps_covered
        checks.push({ item: `skill_${name}_steps_covered`, result: 'WARN', detail: 'steps_covered not found in analysis subagent output — cannot verify multi-method coverage' });
      }

      // 独立分析声明校验：completed skill 的分析结果必须带有独立执行确认
      const confirmationRaw = analysisLog && analysisLog.result
        ? (analysisLog.result.independent_analysis_confirmation || analysisLog.independent_analysis_confirmation)
        : (analysisLog && analysisLog.independent_analysis_confirmation ? analysisLog.independent_analysis_confirmation : null);
      if (skillStatus === 'completed') {
        if (confirmationRaw && typeof confirmationRaw === 'string' && confirmationRaw.trim().length > 0) {
          checks.push({ item: `skill_${name}_independent_analysis`, result: 'PASS', detail: 'independent_analysis_confirmation present' });
        } else {
          checks.push({ item: `skill_${name}_independent_analysis`, result: 'FAIL', detail: 'independent_analysis_confirmation missing or empty — 无法证明分析由独立 subagent 完成' });
          incomplete_skills.push({ subskill: name, unresolved_actions: [], reason: 'independent_analysis_confirmation_missing' });
        }
      }

      // 行为级证据校验：completed 分析结果必须引用真实存在的证据文件（防凭空虚构结论）
      const evidenceSources = analysisLog && analysisLog.result
        ? (analysisLog.result.evidence_sources || analysisLog.evidence_sources)
        : (analysisLog && analysisLog.evidence_sources ? analysisLog.evidence_sources : null);
      if (skillStatus === 'completed' && confirmationRaw) {
        if (Array.isArray(evidenceSources) && evidenceSources.length > 0) {
          const missing = evidenceSources.filter(p => !fs.existsSync(p));
          if (missing.length === 0) {
            checks.push({ item: `skill_${name}_evidence_sources`, result: 'PASS', detail: `${evidenceSources.length} evidence sources all present on disk` });
          } else {
            checks.push({
              item: `skill_${name}_evidence_sources`,
              result: 'FAIL',
              detail: `${missing.length}/${evidenceSources.length} evidence source paths do not exist on disk: ${missing.join(', ')}`,
            });
            incomplete_skills.push({ subskill: name, unresolved_actions: [], reason: 'evidence_sources_missing_on_disk' });
          }
        } else {
          checks.push({
            item: `skill_${name}_evidence_sources`,
            result: 'FAIL',
            detail: 'evidence_sources missing or empty — 分析结论必须引用现场证据文件而非凭空生成',
          });
          incomplete_skills.push({ subskill: name, unresolved_actions: [], reason: 'evidence_sources_missing' });
        }
      }
    }

    const passed = incomplete_skills.length === 0;
    const result = { passed, incomplete_skills, checks };
    this._appendTrace(this.state.current_gate, 'skill-completeness-check', { passed, incomplete_count: incomplete_skills.length });
    this._touch();
    return result;
  }

  /**
   * 返回子 SKILL 的必填手段类别列表。
   * 用于 steps_covered 覆盖校验——分析 subagent 必须覆盖这些类别。
   * @param {string} subskillName
   * @returns {string[]}
   * @private
   */
  _getRequiredCategories(subskillName) {
    const CATEGORY_MAP = {
      'performance-library-selection': [
        'allocators', 'hash_functions', 'compression', 'crypto', 'json',
        'memory_operations', 'pattern_matching', 'linear_algebra',
        'sparse_linear_algebra', 'math', 'dnn', 'fft', 'video',
        'serialization', 'sql_acceleration', 'network', 'kv_storage',
        'ascend_runtime',
      ],
    };
    return CATEGORY_MAP[subskillName] || [];
  }

  // -----------------------------------------------------------------------
  // 执行日志与环境恢复
  // -----------------------------------------------------------------------

  /**
   * 记录一个优化执行步骤及其反向命令。
   * 主 Agent 在每次真实变更执行后必须调用此方法。
   *
   * @param {Object} step
   *   - skill_name:       所属 skill
   *   - round_name:       轮次名 (round-1, round-2, ...)
   *   - action_desc:      人类可读的动作描述
   *   - forward_cmd:      执行的命令
   *   - reverse_cmd:      恢复命令（必须可独立执行）
   *   - type:             restart_service | alter_table | set_global_var | move_irq |
   *                       replace_binary | file_replace | online_param | other
   *   - status:           applied | failed | reverted
   *   - target_host:      命令执行的主机 (默认 localhost)
   */
  recordExecutionStep(step) {
    this._ensureState();
    if (!this.state.execution_log) {
      this.state.execution_log = [];
    }
    this.state.execution_log.push({
      index: this.state.execution_log.length,
      timestamp: new Date().toISOString(),
      skill_name: step.skill_name,
      round_name: step.round_name || '',
      action_desc: step.action_desc || '',
      forward_cmd: step.forward_cmd || '',
      reverse_cmd: step.reverse_cmd || '',
      type: step.type || 'other',
      status: step.status || 'applied',
      target_host: step.target_host || 'localhost',
    });
    this._appendTrace(this.state.current_gate, 'execution-step-recorded', {
      skill: step.skill_name,
      round: step.round_name,
      action: step.action_desc,
      step_count: this.state.execution_log.length,
    });
    this._touch();
    return this.state;
  }

  /**
   * 生成环境恢复计划。
   * 按 LIFO 逆序遍历 execution_log 中 status=applied 的条目，返回 reverse_cmd 列表。
   *
   * @returns {Object} { plan: Array<{index, type, action_desc, reverse_cmd, target_host}>, total_steps, summary }
   */
  generateRestorePlan() {
    this._ensureState();
    const log = this.state.execution_log || [];
    const applied = log.filter(e => e.status === 'applied' && e.reverse_cmd);

    const plan = applied.reverse().map(e => ({
      index: e.index,
      type: e.type,
      skill: e.skill_name,
      round: e.round_name,
      action_desc: e.action_desc,
      reverse_cmd: e.reverse_cmd,
      target_host: e.target_host,
    }));

    const byType = {};
    for (const step of plan) {
      byType[step.type] = (byType[step.type] || 0) + 1;
    }

    return {
      plan,
      total_steps: plan.length,
      ordered_by_lifo: true,
      summary: `${plan.length} 个恢复步骤，按类型分: ${JSON.stringify(byType)}`,
    };
  }

  /**
   * 标记执行步骤为已恢复。
   * @param {number} index  execution_log 中的 index
   */
  markStepReverted(index) {
    this._ensureState();
    const log = this.state.execution_log || [];
    if (log[index]) {
      log[index].status = 'reverted';
      log[index].reverted_at = new Date().toISOString();
    }
    this._touch();
    return this.state;
  }

  // -----------------------------------------------------------------------
  // 迭代状态、耗时与 subagent 日志持久化
  // -----------------------------------------------------------------------

  /**
   * 设置 per_skill_iteration_state（批量更新）。
   * @param {Object} iterationState  { 'skill-name': { status, rounds, round_gains_pct, ... } }
   */
  setIterationState(iterationState) {
    this._ensureState();
    if (!this.state.per_skill_iteration_state) {
      this.state.per_skill_iteration_state = {};
    }

    // ① executor 强制关联校验：状态为 completed/stopped 且声明了轮次时，
    //    必须存在 phase=implementation 且 subagent_type=executor 的实施记录。
    //    防止主 agent 跳过执行验证 subagent 直接手写轮次结果。
    const log = this.state.subagent_invocation_log || [];
    const normalizedIteration = {};
    for (const [key, value] of Object.entries(iterationState)) {
      // 兼容两种格式：{subskill_name: {...}} 或 {subskill: "x", round: n, status: "y"}
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        const subskillKey = value.subskill_name || value.subskill || key;
        normalizedIteration[subskillKey] = value;
      } else if (['subskill', 'subskill_name', 'skill'].includes(key)) {
        normalizedIteration[value] = { ...iterationState };
        delete normalizedIteration[key];
      }
    }
    if (Object.keys(normalizedIteration).length === 0) {
      normalizedIteration[iterationState.subskill || iterationState.subskill_name || ''] = { ...iterationState };
    }
    for (const [subskill, data] of Object.entries(normalizedIteration)) {
      const declaredStatus = data && data.status;
      const hasRound = data && (data.round !== undefined || data.rounds_attempted);
      if (declaredStatus !== 'completed' && declaredStatus !== 'stopped') {
        continue;
      }
      if (!hasRound) {
        continue;
      }
      const executorRecords = log.filter(
        e => e.subskill === subskill && e.phase === 'implementation'
          && (e.subagent_type === 'executor' || e.stage === 'implementation')
      );
      if (executorRecords.length === 0) {
        const err = new Error(
          `[compliance] set-iteration-state rejected: skill "${subskill}" declared ${declaredStatus} but ` +
          `no executor implementation record found in subagent_invocation_log. ` +
          `执行验证必须由独立 executor subagent 完成（append-subagent-log --data '{"subskill":"...","phase":"implementation","subagent_type":"executor","subagent_id":"<real task id>","status":"completed"}'），` +
          `主 agent 禁止自行实施/验证并手写轮次结果。`
        );
        err.code = 'EXECUTOR_MANDATORY_VIOLATION';
        throw err;
      }
    }

    Object.assign(this.state.per_skill_iteration_state, normalizedIteration);
    this._touch();
    return this.state;
  }

  /**
   * 设置 agent_timing_summary、optimization_timing、optimization_timing_details。
   * @param {Object} timing  { agent_timing_summary, optimization_timing, optimization_timing_details }
   */
  setTiming(timing) {
    this._ensureState();
    if (timing.agent_timing_summary) {
      this.state.agent_timing_summary = timing.agent_timing_summary;
    }
    if (timing.optimization_timing) {
      this.state.optimization_timing = timing.optimization_timing;
    }
    if (timing.optimization_timing_details) {
      this.state.optimization_timing_details = timing.optimization_timing_details;
    }
    this._touch();
    return this.state;
  }

  /**
   * 追加 subagent_invocation_log 条目。
   * @param {Object} entry  { subskill, task_path, subagent_id, status, result_path, started_at, ended_at }
   */
  appendSubagentLog(entry) {
    this._ensureState();
    if (!this.state.subagent_invocation_log) {
      this.state.subagent_invocation_log = [];
    }

    // ③ subagent 类型强制校验：OpenCode/Claude Code 等平台 must NOT 降级。
    // 分析阶段 = analyzer（统一分析 agent），执行验证阶段 = executor；subagent_id 必须为真实平台任务 ID。
    const phase = entry.phase || entry.stage || (entry.subagent_type ? undefined : 'analysis');
    const subagentType = entry.subagent_type || inferSubagentType(phase);
    const location = process.env.CLAUDE_CODE_ENTRYPOINT ? 'Claude Code' : 'OpenCode';

    if (!['analyzer', 'executor', 'oracle'].includes(subagentType)) {
      throw new Error(
        `[compliance] append-subagent-log rejected: subagent_type "${subagentType}" invalid. ` +
        `分析阶段必须用 analyzer（统一分析 agent），执行验证阶段必须用 executor。`
      );
    }
    if (phase === 'implementation' && subagentType !== 'executor') {
      throw new Error(
        `[compliance] append-subagent-log rejected: phase=implementation requires subagent_type=executor, got "${subagentType}".`
      );
    }
    if (phase === 'analysis' && subagentType === 'executor') {
      throw new Error(
        `[compliance] append-subagent-log rejected: phase=analysis requires subagent_type=analyzer, got "executor".`
      );
    }
    if (phase === 'implementation' && (!entry.subagent_id || entry.subagent_id === 'platform_unavailable_degraded_context' || !this._isPlausibleSubagentId(entry.subagent_id))) {
      throw new Error(
        `[compliance] append-subagent-log rejected: executor record on ${location} must carry a real platform subagent_id. ` +
        `${location} 禁止降级，必须使用平台 subagent/task 工具启动独立 executor。`
      );
    }
    if (phase === 'analysis' && entry.subagent_id && entry.subagent_id !== 'platform_unavailable_degraded_context' && !this._isPlausibleSubagentId(entry.subagent_id)) {
      throw new Error(
        `[compliance] append-subagent-log rejected: analyzer record subagent_id "${entry.subagent_id}" is not a plausible platform task id. ` +
        `分析 subagent 必须来自平台 subagent/task 工具的真实任务 ID，不得手写伪造 ID。`
      );
    }
    // 同一真实 subagent_id 只能绑定一个 subskill（每次 task 调用独立创建任务）
    if (entry.subagent_id && entry.subagent_id !== 'platform_unavailable_degraded_context') {
      const existed = this.state.subagent_invocation_log.find(
        e => e.subagent_id === entry.subagent_id && e.subskill && entry.subskill && e.subskill !== entry.subskill
      );
      if (existed && existed.subagent_id !== 'platform_unavailable_degraded_context') {
        throw new Error(
          `[compliance] append-subagent-log rejected: subagent_id "${entry.subagent_id}" already bound to subskill "${existed.subskill}", cannot reuse for "${entry.subskill}". ` +
          `每个 skill 必须由独立 task 调用，subagent_id 不得跨 skill 复用。`
        );
      }
    }
    // result_path 若已声明必须真实存在（行为级校验：subagent 产物必须落盘可读）
    if (entry.status === 'completed' && entry.result_path && !fs.existsSync(entry.result_path)) {
      throw new Error(
        `[compliance] append-subagent-log rejected: declared result_path "${entry.result_path}" does not exist on disk. ` +
        `completed 记录必须指向 subagent 实际写入的结果文件。`
      );
    }

    this.state.subagent_invocation_log.push({
      ...entry,
      subagent_type: subagentType,
      recorded_at: new Date().toISOString(),
    });
    this._touch();
    return this.state;
  }

  /**
   * 判断 subagent_id 是否像平台返回的真实任务 ID。
   * 真实平台 ID（OpenCode t开头任务 ID、Claude Code 任务 ID）通常为较长混合字符串；
   * 手写降级/伪造 ID 通常为短词、纯中文、含空格或常见占位词。
   * @private
   */
  _isPlausibleSubagentId(id) {
    if (!id || typeof id !== 'string') return false;
    const trimmed = id.trim();
    if (trimmed.length < 8) return false;
    if (/\s+/.test(trimmed)) return false;
    if (/[\u4e00-\u9fff]/.test(trimmed)) return false;
    // 平台真实 task ID 不含自拟标记。拦截常见自拟/簿记模式（区分大小写宽松），
    // 这些模式曾用于伪造 executor 记录但不来自 task 工具返回值。
    const lower = trimmed.toLowerCase();
    const selfMadePatterns = [
      /\bses[-_](exec|analy|analyse|analysis|oracle|appcfg|app_config|pls|cpuaff)[-_]/, // ses_exec_<skill>_r<n> 自拟ID
      /\bses[-_]executor\b/,
      /\bsubagent[-_](1|2|\d+)\b/,                     // subagent_1
      /\br[0-9]+_\d{8}\b/,                            // _r1_20260820 日期后缀
      /^\d{8}$/,                                      // 纯日期
      /\b(skip|skipped|cancelled|preempted)\b/i,      // 状态词伪装 ID
    ];
    if (selfMadePatterns.some(p => p.test(lower))) return false;
    // 若以平台惯用前缀开头（ses_、agt_、claude 等），要求主体为紧凑随机 hash（无内部下划线分隔的语义片段）
    if (/^ses[-_]/.test(lower) || /^agt[-_]/.test(lower)) {
      const main = trimmed.replace(/^[A-Za-z]{2,5}[-_]/, '');
      if (!/^[a-z0-9]+$/i.test(main)) return false; // 主体中出现 _/日期/语义词 → 自拟 ID
    }
    const fakeTokens = ['manual', 'main-agent', 'mainagent', 'self', 'fake', 'test-id',
      'handwrite', 'direct', 'local', 'placeholder', 'dummy', 'example'];
    if (fakeTokens.some(t => lower === t || lower === t.replace(/[-_]/g, ''))) return false;
    return true;
  }

  /**
   * 设置 per_skill_gain_summary。
   * 同时支持数组格式 [{skill_name, ...}] 和对象格式 {skill_name: {...}}（自动转换）。
   * @param {Array|Object} gains
   */
  setPerSkillGainSummary(gains) {
    this._ensureState();
    // Auto-convert object format {skill_name: {...}} to array [{skill_name, ...}]
    if (gains && !Array.isArray(gains) && typeof gains === 'object') {
      gains = Object.entries(gains).map(([skill_name, data]) => ({
        skill_name,
        ...data,
      }));
    }
    this.state.per_skill_gain_summary = gains;
    this._touch();
    return this.state;
  }

  /**
   * 生成合规摘要（用于最终报告）。包含 compliance + report-input 双重验证结果。
   */
  generateComplianceSummary() {
    const compliance = this.validateCompliance();
    const reportInputs = this.validateReportInputs();
    return {
      dynamic_workflows_compliance: {
        passed: compliance.passed,
        score: compliance.score,
        checks: compliance.checks,
        gate_sequence: GATE_SEQUENCE,
        completed_gates: this.state.completed_gates,
        workflow_trace_count: this.state.workflow_trace.length,
        candidate_count: this.state.candidate_skill_list.length,
        coverage_count: this.state.coverage_skill_list.length,
        summary: compliance.summary,
      },
      report_input_validation: reportInputs,
      workflow_trace: this.state.workflow_trace,
    };
  }

  /**
   * 报告就绪检查（硬门控）。报告生成前必须调用。
   * 同时执行 validateCompliance + validateReportInputs + verifySkillCompleteness，
   * 任一项失败则 ready=false，必须补齐缺失数据后才能生成完成态报告。
   *
   * @returns {{ ready: boolean, compliance: object, report_inputs: object, skill_completeness: object, issues: string[], remediation: string[]|null }}
   */
  reportReady() {
    const compliance = this.validateCompliance();
    const reportInputs = this.validateReportInputs();
    const skillCompleteness = this.verifySkillCompleteness();

    const issues = [];

    // 收集 compliance FAIL 项
    for (const check of compliance.checks) {
      if (check.result === 'FAIL') {
        issues.push(`[compliance] ${check.item}: ${check.detail || 'check failed'}`);
      }
    }

    // 收集 report-inputs FAIL 项
    for (const check of reportInputs.checks) {
      if (check.result === 'FAIL') {
        issues.push(`[report-inputs] ${check.item}: ${check.detail || 'missing required data'}`);
      }
    }

    // 收集 skill-completeness FAIL 项
    for (const check of skillCompleteness.checks) {
      if (check.result === 'FAIL') {
        issues.push(`[skill-completeness] ${check.item}: ${check.detail || 'incomplete skill'}`);
      }
    }
    if (skillCompleteness.incomplete_skills && skillCompleteness.incomplete_skills.length > 0) {
      issues.push(`[skill-completeness] ${skillCompleteness.incomplete_skills.length} skill(s) with unresolved actions: ${skillCompleteness.incomplete_skills.map(s => s.subskill).join(', ')}`);
    }

    const ready = compliance.passed && reportInputs.passed && skillCompleteness.passed;

    const result = {
      ready,
      compliance,
      report_inputs: reportInputs,
      skill_completeness: skillCompleteness,
      issues,
      remediation: ready ? null : this._buildRemediation(compliance, reportInputs, skillCompleteness),
    };

    this._appendTrace(this.state.current_gate, 'report-ready-check', {
      ready,
      compliance_passed: compliance.passed,
      report_inputs_passed: reportInputs.passed,
      skill_completeness_passed: skillCompleteness.passed,
      issue_count: issues.length,
    });
    this._touch();
    return result;
  }

  /**
   * 根据 validate 失败项生成可操作的修复步骤。
   * @private
   */
  _buildRemediation(compliance, reportInputs, skillCompleteness) {
    const steps = [];

    // Compliance 修复建议
    for (const check of compliance.checks) {
      if (check.result !== 'FAIL') continue;
      switch (check.item) {
        case 'report_timing_fields':
          steps.push('执行: node dynamic_workflow_manager.js set-timing --state <state> --data \'{"agent_timing_summary":{...},"optimization_timing":[...],"optimization_timing_details":[...]}\'');
          break;
        case 'workflow_trace_coverage':
          steps.push('执行: node dynamic_workflow_manager.js trace --state <state> --gate <name> --event <name> 补全缺失的 trace 条目');
          break;
        case 'cpu_affinity_first':
          steps.push('cpu-affinity-optimization 必须在所有其他 skill 之前完成。检查 candidate_skill_list 顺序和 per_skill_iteration_state。');
          break;
        default:
          steps.push(`[compliance] 修复检查项 "${check.item}": ${check.detail || '手动检查'}`);
      }
    }

    // Report-input 修复建议
    for (const check of reportInputs.checks) {
      if (check.result !== 'FAIL') continue;
      switch (check.item) {
        case 'agent_timing_summary':
          steps.push('执行: node dynamic_workflow_manager.js set-timing --state <state> --data \'{"agent_timing_summary":{"total_analysis_seconds":...,"total_implementation_seconds":...,"total_validation_seconds":...}}\'');
          break;
        case 'optimization_timing':
          steps.push('执行: node dynamic_workflow_manager.js set-timing --state <state> --data \'{"optimization_timing":[{...}]}\' 补全每轮耗时记录');
          break;
        case 'optimization_timing_details':
          steps.push('执行: node dynamic_workflow_manager.js set-timing --state <state> --data \'{"optimization_timing_details":[{...}]}\' 补全明细记录（至少每条包含 stage/skill_name/round_name/seconds）');
          break;
        case 'subagent_invocation_log':
          steps.push('执行: node dynamic_workflow_manager.js append-subagent-log --state <state> --data \'{"subskill_name":"...","subagent_id":"...","status":"completed",...}\' 为每个 Agent subagent 调用追加日志');
          break;
        case 'per_skill_gain_summary':
          steps.push('执行: node dynamic_workflow_manager.js set-per-skill-gains --state <state> --data \'[{"skill_name":"...","gain_pct":...}]\' (注意：必须是数组格式)');
          break;
        default:
          steps.push(`[report-inputs] 修复检查项 "${check.item}": ${check.detail || '手动补齐数据'}`);
      }
    }

    // Skill-completeness 修复建议
    if (skillCompleteness && skillCompleteness.incomplete_skills) {
      for (const inc of skillCompleteness.incomplete_skills) {
        steps.push(`[skill-completeness] skill "${inc.subskill}" 有 ${inc.unresolved_actions.length} 个未验证动作 (${inc.unresolved_actions.join(', ')})。必须补齐执行验证或显式拒绝（附原因）后重新运行 verify-skill-completeness --state <state> --subskill ${inc.subskill}`);
      }
    }

    return steps;
  }

  // -----------------------------------------------------------------------
  // 持久化
  // -----------------------------------------------------------------------

  /**
   * 保存当前 state 到 JSON 文件。
   */
  save(filePath) {
    this._ensureState();
    const dest = filePath || this.statePath;
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    this._touch();
    fs.writeFileSync(dest, JSON.stringify(this.state, null, 2), 'utf-8');
    return dest;
  }

  // -----------------------------------------------------------------------
  // 内部方法
  // -----------------------------------------------------------------------

  _ensureState() {
    if (!this.state) {
      throw new Error('DynamicWorkflowManager: state not initialized. Call initWorkflowState() or loadState() first.');
    }
  }

  _validateGate(gateName) {
    if (!GATE_SEQUENCE.includes(gateName)) {
      throw new Error(
        `Unknown gate "${gateName}". Must be one of: ${GATE_SEQUENCE.join(', ')}`
      );
    }
  }

  _appendTrace(gate, event, detail = {}) {
    this.state.workflow_trace.push({
      timestamp: new Date().toISOString(),
      gate,
      event,
      ...detail,
    });
  }

  _touch() {
    this.state._updated_at = new Date().toISOString();
  }

  static _validateState(state) {
    const required = ['current_gate', 'completed_gates', 'current_run_id', 'workflow_trace'];
    for (const key of required) {
      if (!(key in state)) {
        throw new Error(`Invalid workflow_state: missing required field "${key}"`);
      }
    }
    if (!GATE_SEQUENCE.includes(state.current_gate)) {
      throw new Error(`Invalid workflow_state: unknown current_gate "${state.current_gate}"`);
    }
    if (!Array.isArray(state.workflow_trace)) {
      throw new Error('Invalid workflow_state: workflow_trace must be an array');
    }
  }
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

function printUsage() {
  console.log(`Usage:
  node dynamic_workflow_manager.js init --run-id <id> --output-dir <dir>
  node dynamic_workflow_manager.js load --state <path>
  node dynamic_workflow_manager.js gate-enter --state <path> --gate <name>
  node dynamic_workflow_manager.js gate-complete --state <path> --gate <name> [--evidence <path>]
  node dynamic_workflow_manager.js gate-block --state <path> --gate <name> --reason <text>
  node dynamic_workflow_manager.js gate-unblock --state <path>
  node dynamic_workflow_manager.js set-evidence --state <path> --status <current|missing|stale|mixed|invalid>
  node dynamic_workflow_manager.js set-candidates --state <path> --candidates <json>
  node dynamic_workflow_manager.js update-candidate-status --state <path> --subskill <name> --status <pending|running|completed|stopped|blocked> [--result <json>]
  node dynamic_workflow_manager.js auto-coverage --state <path>
  node dynamic_workflow_manager.js trace --state <path> --gate <name> --event <name> [--detail <json>]
  node dynamic_workflow_manager.js set-iteration-state --state <path> --data <json>
  node dynamic_workflow_manager.js set-timing --state <path> --data <json>
  node dynamic_workflow_manager.js append-subagent-log --state <path> --data <json>
  node dynamic_workflow_manager.js set-per-skill-gains --state <path> --data <json>   (支持 array/object 双格式)
  node dynamic_workflow_manager.js record-execution --state <path> --data <json>
  node dynamic_workflow_manager.js restore-plan --state <path>
  node dynamic_workflow_manager.js mark-step-reverted --state <path> --index <n>
  node dynamic_workflow_manager.js validate --state <path>
  node dynamic_workflow_manager.js validate-report-inputs --state <path>
  node dynamic_workflow_manager.js verify-evidence-completeness --state <path> [--summary-path <path>] [--allow-degraded] [--perf-status <passed|degraded|failed>]
  node dynamic_workflow_manager.js verify-order --state <path>
  node dynamic_workflow_manager.js verify-skill-completeness --state <path> [--subskill <name>]
  node dynamic_workflow_manager.js report-ready --state <path>       (合并硬门控 — 未就绪则阻塞)
  node dynamic_workflow_manager.js summary --state <path>            (自动调用 report-ready 后才输出)
`);
}

function parseArgv(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const key = argv[i].replace(/^--/, '');
      const val = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
      args[key] = val;
    } else {
      args._.push(argv[i]);
    }
  }
  return args;
}

function main() {
  const args = parseArgv(process.argv.slice(2));
  const cmd = args._[0];

  if (!cmd || cmd === 'help' || args.help) {
    printUsage();
    process.exit(cmd ? 0 : 1);
  }

  try {
    switch (cmd) {
      case 'init': {
        const dwm = new DynamicWorkflowManager(args['run-id'], args['output-dir'] || '.');
        dwm.initWorkflowState();
        const saved = dwm.save();
        console.log(JSON.stringify({ status: 'initialized', saved, state: dwm.state }, null, 2));
        break;
      }

      case 'load': {
        const state = DynamicWorkflowManager.load(args.state);
        console.log(JSON.stringify({ status: 'loaded', state }, null, 2));
        break;
      }

      case 'gate-enter': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        dwm.enterGate(args.gate);
        dwm.save();
        console.log(JSON.stringify({ status: 'entered', gate: args.gate, current_gate: dwm.state.current_gate, next_gate: dwm.state.next_gate }, null, 2));
        break;
      }

      case 'gate-complete': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        dwm.completeGate(args.gate, { evidence_path: args.evidence || null });
        dwm.save();
        console.log(JSON.stringify({ status: 'completed', gate: args.gate, completed_gates: dwm.state.completed_gates, next_gate: dwm.state.next_gate }, null, 2));
        break;
      }

      case 'gate-block': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        dwm.blockGate(args.gate, args.reason || 'unspecified');
        dwm.save();
        console.log(JSON.stringify({ status: 'blocked', gate: args.gate, reason: args.reason }, null, 2));
        break;
      }

      case 'gate-unblock': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        dwm.unblock();
        dwm.save();
        console.log(JSON.stringify({ status: 'unblocked', current_gate: dwm.state.current_gate }, null, 2));
        break;
      }

      case 'set-evidence': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        dwm.setEvidenceStatus(args.status);
        dwm.save();
        console.log(JSON.stringify({ status: 'evidence-updated', evidence_status: dwm.state.evidence_status }, null, 2));
        break;
      }

      case 'set-candidates': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const candidates = JSON.parse(args.candidates);
        dwm.setCandidateSkillList(candidates);
        dwm.save();
        console.log(JSON.stringify({ status: 'candidates-set', count: dwm.state.candidate_skill_list.length }, null, 2));
        break;
      }

      case 'auto-coverage': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        dwm.autoGenerateCoverageList();
        dwm.save();
        console.log(JSON.stringify({ status: 'coverage-generated', count: dwm.state.coverage_skill_list.length, skills: dwm.state.coverage_skill_list.map(c => c.subskill_name) }, null, 2));
        break;
      }

      case 'trace': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const detail = args.detail ? JSON.parse(args.detail) : {};
        dwm.appendTraceEntry(args.gate, args.event, detail);
        dwm.save();
        console.log(JSON.stringify({ status: 'trace-appended', trace_count: dwm.state.workflow_trace.length }, null, 2));
        break;
      }

      case 'validate': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const result = dwm.validateCompliance();
        console.log(JSON.stringify(result, null, 2));
        process.exit(result.passed ? 0 : 1);
        break;
      }

      case 'validate-report-inputs': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const result = dwm.validateReportInputs();
        console.log(JSON.stringify(result, null, 2));
        process.exit(result.passed ? 0 : 1);
        break;
      }

      case 'verify-evidence-completeness': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const result = dwm.verifyEvidenceCompleteness(args['summary-path'], !!args['allow-degraded'], args['perf-status'] || null);
        console.log(JSON.stringify(result, null, 2));
        process.exit(result.passed ? 0 : 1);
        break;
      }

      case 'verify-order': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const result = dwm.verifyOrder();
        console.log(JSON.stringify(result, null, 2));
        process.exit(result.passed ? 0 : 1);
        break;
      }

      case 'verify-skill-completeness': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const result = dwm.verifySkillCompleteness(args.subskill);
        console.log(JSON.stringify(result, null, 2));
        process.exit(result.passed ? 0 : 1);
        break;
      }

      case 'update-candidate-status': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const updateResult = dwm.updateCandidateStatus(args.subskill, args.status, args.result ? JSON.parse(args.result) : {});
        dwm.save();
        console.log(JSON.stringify({ status: 'candidate-updated', subskill: args.subskill, new_status: args.status }, null, 2));
        break;
      }

      case 'set-iteration-state': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const iterState = JSON.parse(args.data);
        dwm.setIterationState(iterState);
        dwm.save();
        console.log(JSON.stringify({ status: 'iteration-state-set', skills: Object.keys(iterState) }, null, 2));
        break;
      }

      case 'set-timing': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const timing = JSON.parse(args.data);
        dwm.setTiming(timing);
        dwm.save();
        console.log(JSON.stringify({ status: 'timing-set' }, null, 2));
        break;
      }

      case 'append-subagent-log': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const entry = JSON.parse(args.data);
        dwm.appendSubagentLog(entry);
        dwm.save();
        console.log(JSON.stringify({ status: 'subagent-log-appended', total: dwm.state.subagent_invocation_log.length }, null, 2));
        break;
      }

      case 'set-per-skill-gains': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const gains = JSON.parse(args.data);
        dwm.setPerSkillGainSummary(gains);
        dwm.save();
        console.log(JSON.stringify({ status: 'gains-set', skills: gains.length }, null, 2));
        break;
      }

      case 'record-execution': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const step = JSON.parse(args.data);
        dwm.recordExecutionStep(step);
        dwm.save();
        console.log(JSON.stringify({ status: 'step-recorded', total_steps: dwm.state.execution_log.length }, null, 2));
        break;
      }

      case 'restore-plan': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const plan = dwm.generateRestorePlan();
        console.log(JSON.stringify(plan, null, 2));
        break;
      }

      case 'mark-step-reverted': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        dwm.markStepReverted(parseInt(args.index, 10));
        dwm.save();
        console.log(JSON.stringify({ status: 'step-marked-reverted', index: parseInt(args.index, 10) }, null, 2));
        break;
      }

      case 'summary': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        // Hard gate: auto-run reportReady() before generating summary.
        // If not ready, output issues and remediation; exit non-zero.
        const ready = dwm.reportReady();
        if (!ready.ready) {
          console.error(JSON.stringify({
            error: 'REPORT_NOT_READY',
            message: '报告未就绪 — 必须先补齐以下缺失数据才能生成完成态报告',
            issues: ready.issues,
            remediation: ready.remediation,
            hint: '修复后重新运行 validate 和 validate-report-inputs 确认通过',
          }, null, 2));
          process.exit(1);
        }
        const summary = dwm.generateComplianceSummary();
        console.log(JSON.stringify(summary, null, 2));
        break;
      }

      case 'report-ready': {
        const dwm = new DynamicWorkflowManager('temp');
        dwm.loadState(args.state);
        const ready = dwm.reportReady();
        console.log(JSON.stringify(ready, null, 2));
        process.exit(ready.ready ? 0 : 1);
        break;
      }

      default:
        console.error(`Unknown command: ${cmd}`);
        printUsage();
        process.exit(1);
    }
  } catch (err) {
    console.error(`Error: ${err.message}`);
    process.exit(1);
  }
}

// 直接运行时进入 CLI
if (require.main === module) {
  main();
}

module.exports = { DynamicWorkflowManager, GATE_SEQUENCE, HARD_GATES, ALL_PRIMARY_SKILLS };
