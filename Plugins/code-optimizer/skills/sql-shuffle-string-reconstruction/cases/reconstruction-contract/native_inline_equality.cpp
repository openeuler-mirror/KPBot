// Target-side regression: compile inside the configured Velox build, with its
// actual StringView header and dependencies. This is not a substitute ABI.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <stdexcept>

#include "velox/type/StringView.h"

using facebook::velox::StringView;

StringView rebuildInline(
    const std::array<char, 16>& input,
    int32_t length,
    bool clearPadding) {
  StringView output(input.data(), length);
  auto* bytes = reinterpret_cast<unsigned char*>(&output);
  const auto* data = reinterpret_cast<const unsigned char*>(output.data());
  const auto offset = static_cast<size_t>(data - bytes);
  if (length < 0 || !output.isInline() ||
      offset + StringView::kInlineSize > sizeof(output)) {
    throw std::runtime_error("unexpected inline layout");
  }
  std::array<char, 16> loaded;
  std::memcpy(loaded.data(), input.data(), loaded.size());
  std::memcpy(bytes + offset, loaded.data(), StringView::kInlineSize);
  if (clearPadding) {
    std::memset(bytes + offset + length, 0, StringView::kInlineSize - length);
  }
  return output;
}

int main() {
  static_assert(sizeof(StringView) == 16);
  bool canonical = true;
  for (int32_t length = 0; length <= static_cast<int32_t>(StringView::kInlineSize); ++length) {
    std::array<char, 16> left;
    std::array<char, 16> right;
    left.fill('X');
    right.fill('Y');
    for (int32_t index = 0; index < length; ++index) {
      left[index] = right[index] = static_cast<char>('a' + index);
    }
    const auto a = rebuildInline(left, length, true);
    const auto b = rebuildInline(right, length, true);
    const StringView reference(left.data(), length);
    canonical = canonical && a == b && a == reference;
    const auto* payload = reinterpret_cast<const unsigned char*>(a.data());
    canonical = canonical && std::all_of(
        payload + length, payload + StringView::kInlineSize,
        [](unsigned char byte) { return byte == 0; });
  }
  std::array<char, 16> left;
  std::array<char, 16> right;
  left.fill('X');
  right.fill('Y');
  left[0] = right[0] = 'a';
  const auto dirtyLeft = rebuildInline(left, 1, false);
  const auto dirtyRight = rebuildInline(right, 1, false);
  const bool counterexample = !(dirtyLeft == dirtyRight);
  const bool success = canonical && counterexample;
  std::cout << "{\"success\":" << (success ? "true" : "false")
            << ",\"native_StringView_operator_equal\":true}\n";
  return success ? 0 : 1;
}
