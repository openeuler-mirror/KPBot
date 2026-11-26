//===- IssueTraceView.h -----------------------------------------*- C++ -*-===//
//
// Records the concrete processor resource units consumed by every issued
// instruction (per iteration), together with the issue cycle. Reserved
// resource groups (no concrete unit is selected by MCA) are recorded
// separately with their member units.
//
//===----------------------------------------------------------------------===//

#ifndef LLVM_TOOLS_LLVM_MCA_ISSUETRACEVIEW_H
#define LLVM_TOOLS_LLVM_MCA_ISSUETRACEVIEW_H

#include "llvm/ADT/ArrayRef.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/MC/MCInst.h"
#include "llvm/MC/MCSubtargetInfo.h"
#include "llvm/MCA/View.h"
#include "llvm/Support/JSON.h"
#include <cstdint>
#include <string>
#include <vector>

namespace llvm {
namespace mca {

class IssueTraceView : public View {
  struct ResUse {
    std::string Kind;
    unsigned Unit;
    double Cycles;
  };

  struct Record {
    unsigned SrcIdx;
    unsigned Cycle;
    unsigned Iteration;
    unsigned Index;
    SmallVector<ResUse, 4> Uses;
  };

  struct ReservedRecord {
    unsigned SrcIdx;
    unsigned Cycle;
    unsigned Iteration;
    unsigned Index;
    std::string Group;
    uint64_t Mask;
    unsigned Cycles;
    SmallVector<std::string, 4> Members;
  };

  const MCSubtargetInfo &STI;
  unsigned NumInstr;
  unsigned CurrentCycle;
  std::vector<Record> Records;
  std::vector<ReservedRecord> Reserved;
  std::vector<unsigned> DispatchCycles;
  std::vector<unsigned> RetireCycles;
  std::vector<uint64_t> ProcResID2Mask;

  void collectGroupMembers(unsigned KindIdx, SmallVectorImpl<std::string> &Out) const;

public:
  IssueTraceView(const MCSubtargetInfo &STI, ArrayRef<MCInst> S);

  StringRef getNameAsString() const override { return "IssueTraceView"; }
  void onCycleEnd() override { ++CurrentCycle; }
  void onEvent(const HWInstructionEvent &Event) override;
  void printView(raw_ostream &OS) const override;
  json::Value toJSON() const override;
};

} // namespace mca
} // namespace llvm

#endif // LLVM_TOOLS_LLVM_MCA_ISSUETRACEVIEW_H
