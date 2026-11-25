#include "IssueTraceView.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/MCA/Instruction.h"
#include "llvm/MCA/Support.h"
#include "llvm/Support/MathExtras.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
using namespace llvm::mca;

IssueTraceView::IssueTraceView(const MCSubtargetInfo &STI, ArrayRef<MCInst> S)
    : STI(STI), NumInstr(S.size()), CurrentCycle(0) {
  const MCSchedModel &SM = STI.getSchedModel();
  ProcResID2Mask.resize(SM.getNumProcResourceKinds());
  computeProcResourceMasks(SM, ProcResID2Mask);
}

void IssueTraceView::collectGroupMembers(unsigned KindIdx,
                                         SmallVectorImpl<std::string> &Out) const {
  const MCSchedModel &SM = STI.getSchedModel();
  const MCProcResourceDesc *GD = SM.getProcResource(KindIdx);
  if (!GD || !GD->SubUnitsIdxBegin)
    return;
  // TableGen emits SubUnitsIdxBegin with one entry per unit (multi-unit
  // members are repeated). The occurrence index is the unit index.
  DenseMap<unsigned, unsigned> Occ;
  for (unsigned I = 0; I < GD->NumUnits; ++I) {
    unsigned SubIdx = GD->SubUnitsIdxBegin[I];
    const MCProcResourceDesc *SD = SM.getProcResource(SubIdx);
    if (!SD)
      continue;
    unsigned Unit = Occ[SubIdx]++;
    std::string Name = SD->Name;
    if (SD->NumUnits > 1) {
      Name += ".";
      Name += std::to_string(Unit);
    }
    Out.push_back(std::move(Name));
  }
}

void IssueTraceView::onEvent(const HWInstructionEvent &Event) {
  const unsigned SrcIdx = Event.IR.getSourceIndex();

  if (Event.Type == HWInstructionEvent::Dispatched) {
    if (DispatchCycles.size() <= SrcIdx)
      DispatchCycles.resize(SrcIdx + 1, 0);
    DispatchCycles[SrcIdx] = CurrentCycle;
    return;
  }

  if (Event.Type == HWInstructionEvent::Retired) {
    if (RetireCycles.size() <= SrcIdx)
      RetireCycles.resize(SrcIdx + 1, 0);
    RetireCycles[SrcIdx] = CurrentCycle;
    return;
  }

  if (Event.Type != HWInstructionEvent::Issued)
    return;

  const auto &IE = static_cast<const HWInstructionIssuedEvent &>(Event);
  const unsigned Iteration = NumInstr ? SrcIdx / NumInstr : 0;
  const unsigned Index = NumInstr ? SrcIdx % NumInstr : 0;
  const MCSchedModel &SM = STI.getSchedModel();

  Record R;
  R.SrcIdx = SrcIdx;
  R.Cycle = CurrentCycle;
  R.Iteration = Iteration;
  R.Index = Index;
  for (const ResourceUse &RU : IE.UsedResources) {
    const unsigned KindIdx = static_cast<unsigned>(RU.first.first);
    const uint64_t Mask = RU.first.second;
    const MCProcResourceDesc *Desc = SM.getProcResource(KindIdx);
    if (!Desc)
      continue;
    unsigned Unit = Mask ? static_cast<unsigned>(countr_zero(Mask)) : 0;
    std::string Name = Desc->Name;
    if (Desc->NumUnits > 1) {
      Name += ".";
      Name += std::to_string(Unit);
    }
    R.Uses.push_back({Name, Unit, static_cast<double>(RU.second)});
  }
  Records.push_back(std::move(R));

  // Reserved resource groups do not select a concrete unit. Enumerate the
  // affected member units from the group mask.
  const Instruction *Inst = IE.IR.getInstruction();
  if (!Inst)
    return;
  const InstrDesc &D = Inst->getDesc();
  for (const std::pair<uint64_t, ResourceUsage> &RU : D.Resources) {
    if (!RU.second.isReserved())
      continue;
    ReservedRecord RR;
    RR.SrcIdx = SrcIdx;
    RR.Cycle = CurrentCycle;
    RR.Iteration = Iteration;
    RR.Index = Index;
    RR.Mask = RU.first;
    RR.Cycles = RU.second.size();

    unsigned KindIdx = 0;
    for (unsigned K = 1, E = ProcResID2Mask.size(); K < E; ++K) {
      if (ProcResID2Mask[K] == RU.first) {
        KindIdx = K;
        break;
      }
    }
    const MCProcResourceDesc *GD =
        KindIdx ? SM.getProcResource(KindIdx) : nullptr;
    RR.Group = GD ? GD->Name : ("MASK:" + utohexstr(RU.first));
    if (KindIdx)
      collectGroupMembers(KindIdx, RR.Members);
    Reserved.push_back(std::move(RR));
  }
}

void IssueTraceView::printView(raw_ostream &OS) const {
  OS << "\n\n#ISSUE_TRACE v1\n";
  OS << "cycle,iter,index,dispatch,retire,kind,unit,cycles\n";
  for (const Record &R : Records) {
    const unsigned D =
        R.SrcIdx < DispatchCycles.size() ? DispatchCycles[R.SrcIdx] : 0;
    const unsigned Rt =
        R.SrcIdx < RetireCycles.size() ? RetireCycles[R.SrcIdx] : 0;
    for (const ResUse &U : R.Uses)
      OS << R.Cycle << ',' << R.Iteration << ',' << R.Index << ',' << D << ','
         << Rt << ',' << U.Kind << ',' << U.Unit << ',' << U.Cycles << '\n';
  }

  if (!Reserved.empty()) {
    OS << "\n#RESERVED v1\ncycle,iter,index,group,mask,cycles,members\n";
    for (const ReservedRecord &R : Reserved) {
      OS << R.Cycle << ',' << R.Iteration << ',' << R.Index << ',' << R.Group
         << ',' << utohexstr(R.Mask) << ',' << R.Cycles << ',';
      bool First = true;
      for (const std::string &M : R.Members) {
        if (!First)
          OS << '|';
        OS << M;
        First = false;
      }
      OS << '\n';
    }
  }

  // Diagnostic: list every processor resource group and its member units.
  const MCSchedModel &SM = STI.getSchedModel();
  OS << "\n#GROUPS v1\nname,members\n";
  for (unsigned K = 1, E = SM.getNumProcResourceKinds(); K < E; ++K) {
    const MCProcResourceDesc *GD = SM.getProcResource(K);
    if (!GD || !GD->SubUnitsIdxBegin)
      continue;
    OS << GD->Name << ',';
    SmallVector<std::string, 8> Members;
    collectGroupMembers(K, Members);
    bool First = true;
    for (const std::string &M : Members) {
      if (!First)
        OS << '|';
      OS << M;
      First = false;
    }
    OS << '\n';
  }
}

json::Value IssueTraceView::toJSON() const {
  json::Array Recs;
  for (const Record &R : Records) {
    json::Array Uses;
    for (const ResUse &U : R.Uses)
      Uses.push_back(json::Object(
          {{"Kind", U.Kind}, {"Unit", U.Unit}, {"Cycles", U.Cycles}}));
    const unsigned D =
        R.SrcIdx < DispatchCycles.size() ? DispatchCycles[R.SrcIdx] : 0;
    const unsigned Rt =
        R.SrcIdx < RetireCycles.size() ? RetireCycles[R.SrcIdx] : 0;
    Recs.push_back(json::Object({{"Cycle", R.Cycle},
                                 {"Iteration", R.Iteration},
                                 {"Index", R.Index},
                                 {"DispatchCycle", D},
                                 {"RetireCycle", Rt},
                                 {"Resources", std::move(Uses)}}));
  }

  json::Array Rsv;
  for (const ReservedRecord &R : Reserved) {
    json::Array Members;
    for (const std::string &M : R.Members)
      Members.push_back(M);
    Rsv.push_back(json::Object({{"Cycle", R.Cycle},
                                {"Iteration", R.Iteration},
                                {"Index", R.Index},
                                {"Group", R.Group},
                                {"Mask", utohexstr(R.Mask)},
                                {"Cycles", R.Cycles},
                                {"Members", std::move(Members)}}));
  }

  return json::Object({{"Instructions", NumInstr},
                       {"Records", std::move(Recs)},
                       {"Reserved", std::move(Rsv)}});
}
