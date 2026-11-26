// Tier-B bit-exactness and A/B benchmark case for the
// go-protobuf-fixed-slice-bulk-copy skill.
//
// The two implementations below are self-contained replicas of the skill's
// documented templates (no protobuf-go dependency):
//
//   - decodeFloat32PackedOld / encodeFloat32PackedOld replicate the
//     protobuf-go 1.36.12 packed branch line by line, including the
//     growFloat32Slice exact preallocation: grow writes the field through
//     the pointer FIRST, and the error return skips the "*sp = s" writeback,
//     so a malformed payload on a nil field leaves a non-nil len0 slice.
//   - decodeFloat32PackedNew / encodeFloat32PackedNew are the skill's
//     rewritten codec: the little-endian fast path enters only on
//     wire_size-aligned non-empty payloads; big-endian, empty and malformed
//     lengths all fall through to the original path, keeping the error
//     value AND the destination field state byte-identical with the old path.
package bitexactbench

import (
	"bytes"
	"encoding/binary"
	"errors"
	"math"
	"math/rand"
	"strconv"
	"testing"
	"unsafe"
)

var errDecode = errors.New("proto: cannot parse invalid wire-format data")

// Endian gate: byte probe only. Never compare NativeEndian with LittleEndian
// (direct comparison of the two concrete types does not compile; through the
// binary.ByteOrder interface it compiles but is always false, silently
// disabling the fast path with zero test failures).
var isLittleEndian = func() bool {
	var x uint32 = 1
	return (*[4]byte)(unsafe.Pointer(&x))[0] == 1
}()

// ---------------------------------------------------------------------------
// float32 codec (fixed32 family, wire_size = 4)
// ---------------------------------------------------------------------------

// growFloat32Slice mirrors protobuf-go pointer_unsafe.go growInt32Slice: new
// backing array sized exactly for the appended count, written back through
// the destination pointer BEFORE the decode loop runs.
func growFloat32Slice(sp *[]float32, count int) {
	grown := make([]float32, 0, count+len(*sp))
	grown = grown[:len(*sp)]
	copy(grown, *sp)
	*sp = grown
}

func appendFixed32(b []byte, v uint32) []byte {
	return append(b, byte(v), byte(v>>8), byte(v>>16), byte(v>>24))
}

// encodeFloat32PackedOld is the old per-element marshal loop (LE byte order
// is the wire format regardless of host endianness).
func encodeFloat32PackedOld(b []byte, s []float32) []byte {
	for _, v := range s {
		b = appendFixed32(b, math.Float32bits(v))
	}
	return b
}

func appendFloat32SliceLE(b []byte, s []float32) []byte {
	return append(b, unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s))), 4*len(s))...)
}

func encodeFloat32PackedNew(b []byte, s []float32) []byte {
	if isLittleEndian {
		return appendFloat32SliceLE(b, s)
	}
	return encodeFloat32PackedOld(b, s)
}

// decodeFloat32PackedOld replicates the old packed branch: exact grow through
// the pointer first, then per-element consumption; the trailing failure
// returns WITHOUT the "*sp = s" writeback, leaving the grown field state.
func decodeFloat32PackedOld(sp *[]float32, payload []byte) error {
	count := len(payload) / 4
	if count > 0 {
		growFloat32Slice(sp, count)
	}
	s := *sp
	for len(payload) > 0 {
		if len(payload) < 4 {
			return errDecode
		}
		v := binary.LittleEndian.Uint32(payload[:4])
		s = append(s, math.Float32frombits(v))
		payload = payload[4:]
	}
	*sp = s
	return nil
}

func extendFloat32SliceLE(s []float32, data []byte) []float32 {
	oldLen := len(s)
	s = s[:oldLen+len(data)/4] // grow has reserved exactly this capacity
	copy(unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s[oldLen:]))), len(data)), data)
	return s
}

// decodeFloat32PackedNew is the rewritten codec: the LE fast path enters only
// on 4-byte-aligned non-empty payloads; malformed lengths fall back to the
// original path instead of failing early, preserving error value and field
// final state bit-for-bit.
func decodeFloat32PackedNew(sp *[]float32, payload []byte) error {
	if isLittleEndian && len(payload) > 0 && len(payload)%4 == 0 {
		growFloat32Slice(sp, len(payload)/4)
		*sp = extendFloat32SliceLE(*sp, payload)
		return nil
	}
	return decodeFloat32PackedOld(sp, payload)
}

// ---------------------------------------------------------------------------
// float64 codec (fixed64 family, wire_size = 8)
// ---------------------------------------------------------------------------

func growFloat64Slice(sp *[]float64, count int) {
	grown := make([]float64, 0, count+len(*sp))
	grown = grown[:len(*sp)]
	copy(grown, *sp)
	*sp = grown
}

func appendFixed64(b []byte, v uint64) []byte {
	return append(b, byte(v), byte(v>>8), byte(v>>16), byte(v>>24),
		byte(v>>32), byte(v>>40), byte(v>>48), byte(v>>56))
}

func encodeFloat64PackedOld(b []byte, s []float64) []byte {
	for _, v := range s {
		b = appendFixed64(b, math.Float64bits(v))
	}
	return b
}

func appendFloat64SliceLE(b []byte, s []float64) []byte {
	return append(b, unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s))), 8*len(s))...)
}

func encodeFloat64PackedNew(b []byte, s []float64) []byte {
	if isLittleEndian {
		return appendFloat64SliceLE(b, s)
	}
	return encodeFloat64PackedOld(b, s)
}

func decodeFloat64PackedOld(sp *[]float64, payload []byte) error {
	count := len(payload) / 8
	if count > 0 {
		growFloat64Slice(sp, count)
	}
	s := *sp
	for len(payload) > 0 {
		if len(payload) < 8 {
			return errDecode
		}
		v := binary.LittleEndian.Uint64(payload[:8])
		s = append(s, math.Float64frombits(v))
		payload = payload[8:]
	}
	*sp = s
	return nil
}

func extendFloat64SliceLE(s []float64, data []byte) []float64 {
	oldLen := len(s)
	s = s[:oldLen+len(data)/8]
	copy(unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(s[oldLen:]))), len(data)), data)
	return s
}

func decodeFloat64PackedNew(sp *[]float64, payload []byte) error {
	if isLittleEndian && len(payload) > 0 && len(payload)%8 == 0 {
		growFloat64Slice(sp, len(payload)/8)
		*sp = extendFloat64SliceLE(*sp, payload)
		return nil
	}
	return decodeFloat64PackedOld(sp, payload)
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func float32BitPatterns() []uint32 {
	patterns := []uint32{
		0x00000000, 0x80000000, // +0 / -0
		0x3F800000, 0xBF800000, // 1.0 / -1.0
		0x7F800000, 0xFF800000, // +Inf / -Inf
		0x7FC00000,             // quiet NaN
		0x7F800001, 0xFFC00001, // signaling / negative NaN payload
		0x00000001, 0x007FFFFF, // min/max subnormal
		0x7F7FFFFF,             // max normal
		0xFFFFFFFF,             // NaN payload
	}
	r := rand.New(rand.NewSource(42))
	for i := 0; i < 32; i++ {
		patterns = append(patterns, r.Uint32())
	}
	return patterns
}

func float64BitPatterns() []uint64 {
	patterns := []uint64{
		0x0000000000000000, 0x8000000000000000, // +0 / -0
		0x3FF0000000000000, 0xBFF0000000000000, // 1.0 / -1.0
		0x7FF0000000000000, 0xFFF0000000000000, // +Inf / -Inf
		0x7FF8000000000000,             // quiet NaN
		0x7FF8000000000001, 0xFFF8000000000001, // signaling / negative NaN payload
		0x0000000000000001, 0x000FFFFFFFFFFFFF, // min/max subnormal
		0x7FEFFFFFFFFFFFFF,             // max normal
		0xFFFFFFFFFFFFFFFF,             // NaN payload
	}
	r := rand.New(rand.NewSource(42))
	for i := 0; i < 32; i++ {
		patterns = append(patterns, r.Uint64())
	}
	return patterns
}

func sliceState32(s []float32) (isNil bool, length, capacity int, bits []uint32) {
	isNil = s == nil
	length, capacity = len(s), cap(s)
	for _, v := range s {
		bits = append(bits, math.Float32bits(v))
	}
	return
}

func sliceState64(s []float64) (isNil bool, length, capacity int, bits []uint64) {
	isNil = s == nil
	length, capacity = len(s), cap(s)
	for _, v := range s {
		bits = append(bits, math.Float64bits(v))
	}
	return
}

func assertFieldState32Equal(t *testing.T, label string, oldS, newS []float32) {
	t.Helper()
	on, ol, oc, ob := sliceState32(oldS)
	nn, nl, nc, nb := sliceState32(newS)
	if on != nn || ol != nl || oc != nc || !equalBits32(ob, nb) {
		t.Fatalf("%s: field state mismatch old(nil=%v len=%d cap=%d) new(nil=%v len=%d cap=%d)",
			label, on, ol, oc, nn, nl, nc)
	}
}

func assertFieldState64Equal(t *testing.T, label string, oldS, newS []float64) {
	t.Helper()
	on, ol, oc, ob := sliceState64(oldS)
	nn, nl, nc, nb := sliceState64(newS)
	if on != nn || ol != nl || oc != nc || !equalBits64(ob, nb) {
		t.Fatalf("%s: field state mismatch old(nil=%v len=%d cap=%d) new(nil=%v len=%d cap=%d)",
			label, on, ol, oc, nn, nl, nc)
	}
}

func equalBits32(a, b []uint32) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func equalBits64(a, b []uint64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------

func TestEndianProbeMatchesHost(t *testing.T) {
	var x uint32 = 0x01020304
	nativeFirstByte := (*[4]byte)(unsafe.Pointer(&x))[0]
	hostLE := nativeFirstByte == 0x04
	if isLittleEndian != hostLE {
		t.Fatalf("endian probe %v disagrees with host byte order %v", isLittleEndian, hostLE)
	}
	if isLittleEndian {
		t.Log("little-endian host: LE fast path is active")
	} else {
		t.Log("big-endian host: fast path falls back; parity tests still hold")
	}
}

func TestMarshalFloat32BitExact(t *testing.T) {
	patterns := float32BitPatterns()
	for _, n := range []int{0, 1, 3, 17, len(patterns)} {
		s := make([]float32, n)
		for i := range s {
			s[i] = math.Float32frombits(patterns[i%len(patterns)])
		}
		old := encodeFloat32PackedOld(nil, s)
		newB := encodeFloat32PackedNew(nil, s)
		if !bytes.Equal(old, newB) {
			t.Fatalf("marshal mismatch for n=%d: old %d bytes, new %d bytes", n, len(old), len(newB))
		}
	}
}

func TestUnmarshalFloat32BitExact(t *testing.T) {
	patterns := float32BitPatterns()
	src := make([]float32, len(patterns))
	for i, bits := range patterns {
		src[i] = math.Float32frombits(bits)
	}
	payload := encodeFloat32PackedOld(nil, src)

	var oldS, newS []float32
	if err := decodeFloat32PackedOld(&oldS, payload); err != nil {
		t.Fatalf("old decode failed: %v", err)
	}
	if err := decodeFloat32PackedNew(&newS, payload); err != nil {
		t.Fatalf("new decode failed: %v", err)
	}
	if len(oldS) != len(src) || len(newS) != len(src) {
		t.Fatalf("decode length mismatch: old=%d new=%d want=%d", len(oldS), len(newS), len(src))
	}
	for i := range src {
		want := math.Float32bits(src[i])
		if math.Float32bits(oldS[i]) != want || math.Float32bits(newS[i]) != want {
			t.Fatalf("bit mismatch at %d: want %#x old %#x new %#x",
				i, want, math.Float32bits(oldS[i]), math.Float32bits(newS[i]))
		}
	}
}

func TestMarshalFloat64BitExact(t *testing.T) {
	patterns := float64BitPatterns()
	for _, n := range []int{0, 1, 3, 17, len(patterns)} {
		s := make([]float64, n)
		for i := range s {
			s[i] = math.Float64frombits(patterns[i%len(patterns)])
		}
		old := encodeFloat64PackedOld(nil, s)
		newB := encodeFloat64PackedNew(nil, s)
		if !bytes.Equal(old, newB) {
			t.Fatalf("marshal mismatch for n=%d: old %d bytes, new %d bytes", n, len(old), len(newB))
		}
	}
}

func TestUnmarshalFloat64BitExact(t *testing.T) {
	patterns := float64BitPatterns()
	src := make([]float64, len(patterns))
	for i, bits := range patterns {
		src[i] = math.Float64frombits(bits)
	}
	payload := encodeFloat64PackedOld(nil, src)

	var oldS, newS []float64
	if err := decodeFloat64PackedOld(&oldS, payload); err != nil {
		t.Fatalf("old decode failed: %v", err)
	}
	if err := decodeFloat64PackedNew(&newS, payload); err != nil {
		t.Fatalf("new decode failed: %v", err)
	}
	if len(oldS) != len(src) || len(newS) != len(src) {
		t.Fatalf("decode length mismatch: old=%d new=%d want=%d", len(oldS), len(newS), len(src))
	}
	for i := range src {
		want := math.Float64bits(src[i])
		if math.Float64bits(oldS[i]) != want || math.Float64bits(newS[i]) != want {
			t.Fatalf("bit mismatch at %d: want %#x old %#x new %#x",
				i, want, math.Float64bits(oldS[i]), math.Float64bits(newS[i]))
		}
	}
}

// TestMalformedFloat32FieldStateParity guards the review finding: a malformed
// payload on a nil field must leave the SAME field state in both paths. The
// old path grows first (nil -> non-nil len0 capN) and its trailing failure
// returns before the writeback; the new path must fall back to it instead of
// failing early in the fast path (which would leave the field nil).
func TestMalformedFloat32FieldStateParity(t *testing.T) {
	wellFormed := appendFixed32(nil, 0x3F800000)
	// 5 bytes: one full element + a 1-byte malformed tail.
	payload5 := append(wellFormed, 0x7F)

	var oldS, newS []float32
	errOld := decodeFloat32PackedOld(&oldS, payload5)
	errNew := decodeFloat32PackedNew(&newS, payload5)
	if errOld == nil || errNew == nil || errOld != errNew {
		t.Fatalf("error mismatch for 5-byte payload: old=%v new=%v", errOld, errNew)
	}
	if oldS == nil || len(oldS) != 0 || cap(oldS) != 1 {
		t.Fatalf("old path state changed: want non-nil len0 cap1, got nil=%v len=%d cap=%d",
			oldS == nil, len(oldS), cap(oldS))
	}
	assertFieldState32Equal(t, "5-byte payload", oldS, newS)
	if newS == nil {
		t.Fatalf("new path left the field nil: malformed payloads must fall back to the original path")
	}

	// 3 bytes: count=0, no grow, immediate failure -> field stays nil in both.
	var oldNil, newNil []float32
	errOld = decodeFloat32PackedOld(&oldNil, appendFixed32(nil, 0x3F800000)[:3])
	errNew = decodeFloat32PackedNew(&newNil, appendFixed32(nil, 0x3F800000)[:3])
	if errOld == nil || errNew == nil || errOld != errNew {
		t.Fatalf("error mismatch for 3-byte payload: old=%v new=%v", errOld, errNew)
	}
	assertFieldState32Equal(t, "3-byte payload", oldNil, newNil)
	if oldNil != nil || newNil != nil {
		t.Fatalf("3-byte payload must leave the field nil in both paths")
	}

	// Malformed tail on a non-nil accumulated field: parity again.
	base := []float32{1.5, -2.5}
	oldAcc, newAcc := append([]float32(nil), base...), append([]float32(nil), base...)
	if errOld := decodeFloat32PackedOld(&oldAcc, payload5); errOld == nil {
		t.Fatal("expected errDecode for malformed payload on accumulated field")
	}
	if errNew := decodeFloat32PackedNew(&newAcc, payload5); errNew == nil {
		t.Fatal("expected errDecode for malformed payload on accumulated field (new)")
	}
	assertFieldState32Equal(t, "accumulated field", oldAcc, newAcc)
}

// TestMalformedFloat64Boundary covers the fixed64 family: 4 bytes is already
// malformed for wire_size=8 (it would slip past a "not a multiple of 4"
// check), so the fast-path gate must fall back to the original path and both
// must fail identically.
func TestMalformedFloat64Boundary(t *testing.T) {
	for _, size := range []int{4, 12} {
		payload := make([]byte, size)
		for i := range payload {
			payload[i] = byte(0x3F * (i + 1))
		}
		var oldS, newS []float64
		errOld := decodeFloat64PackedOld(&oldS, payload)
		errNew := decodeFloat64PackedNew(&newS, payload)
		if errOld == nil || errNew == nil || errOld != errNew {
			t.Fatalf("wire_size=8: %d-byte payload: old=%v new=%v", size, errOld, errNew)
		}
		assertFieldState64Equal(t, "fixed64 malformed payload", oldS, newS)
	}

	// 16 bytes: well-formed, two elements, no error in both paths.
	payload := appendFixed64(nil, 0x3FF0000000000000)
	payload = appendFixed64(payload, 0xBFF0000000000000)
	var oldS, newS []float64
	if err := decodeFloat64PackedOld(&oldS, payload); err != nil {
		t.Fatalf("old decode of well-formed 16-byte payload failed: %v", err)
	}
	if err := decodeFloat64PackedNew(&newS, payload); err != nil {
		t.Fatalf("new decode of well-formed 16-byte payload failed: %v", err)
	}
	if len(oldS) != 2 || len(newS) != 2 {
		t.Fatalf("decoded lengths: old=%d new=%d want=2", len(oldS), len(newS))
	}
	assertFieldState64Equal(t, "fixed64 well-formed payload", oldS, newS)
}

func TestEmptyPayloadNoop(t *testing.T) {
	var oldNil, newNil []float32
	if err := decodeFloat32PackedOld(&oldNil, nil); err != nil {
		t.Fatalf("old: empty payload must not error: %v", err)
	}
	if err := decodeFloat32PackedNew(&newNil, nil); err != nil {
		t.Fatalf("new: empty payload must not error: %v", err)
	}
	assertFieldState32Equal(t, "empty payload, nil field", oldNil, newNil)

	oldAcc := []float32{1.5}
	newAcc := []float32{1.5}
	if err := decodeFloat32PackedOld(&oldAcc, nil); err != nil {
		t.Fatalf("old: empty payload on accumulated field: %v", err)
	}
	if err := decodeFloat32PackedNew(&newAcc, nil); err != nil {
		t.Fatalf("new: empty payload on accumulated field: %v", err)
	}
	if len(oldAcc) != 1 || len(newAcc) != 1 {
		t.Fatalf("empty payload changed length: old=%d new=%d", len(oldAcc), len(newAcc))
	}
	assertFieldState32Equal(t, "empty payload, accumulated field", oldAcc, newAcc)
}

func TestMultiEntryAccumulation(t *testing.T) {
	entries := [][]float32{
		{math.Float32frombits(0x3F800000), math.Float32frombits(0xFFC00001)},
		{},
		{math.Float32frombits(0x7F800001), math.Float32frombits(0x00000001), math.Float32frombits(0x7F7FFFFF)},
	}
	var oldS, newS []float32
	var expected []float32
	for _, e := range entries {
		payload := encodeFloat32PackedOld(nil, e)
		if err := decodeFloat32PackedOld(&oldS, payload); err != nil {
			t.Fatalf("old: entry decode failed: %v", err)
		}
		if err := decodeFloat32PackedNew(&newS, payload); err != nil {
			t.Fatalf("new: entry decode failed: %v", err)
		}
		expected = append(expected, e...)
		assertFieldState32Equal(t, "accumulation", oldS, newS)
	}
	for i := range expected {
		if math.Float32bits(oldS[i]) != math.Float32bits(expected[i]) ||
			math.Float32bits(newS[i]) != math.Float32bits(expected[i]) {
			t.Fatalf("accumulated bit mismatch at %d", i)
		}
	}
}

// ---------------------------------------------------------------------------
// A/B benchmarks (honest protocol: the old replica keeps the exact
// preallocation; numbers are recorded evidence, not a gate)
// ---------------------------------------------------------------------------

func benchFloats(n int) []float32 {
	r := rand.New(rand.NewSource(7))
	s := make([]float32, n)
	for i := range s {
		s[i] = r.Float32()
	}
	return s
}

func benchmarkMarshal32(b *testing.B, newImpl bool) {
	for _, n := range []int{16, 4096, 65536} {
		b.Run(map[bool]string{true: "new", false: "old"}[newImpl]+"/"+strconv.Itoa(n), func(b *testing.B) {
			s := benchFloats(n)
			out := make([]byte, 4*n)
			b.SetBytes(int64(4 * n))
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if newImpl {
					_ = encodeFloat32PackedNew(out[:0], s)
				} else {
					_ = encodeFloat32PackedOld(out[:0], s)
				}
			}
		})
	}
}

func BenchmarkMarshalFloat32Old(b *testing.B) { benchmarkMarshal32(b, false) }
func BenchmarkMarshalFloat32New(b *testing.B) { benchmarkMarshal32(b, true) }

func benchmarkUnmarshal32(b *testing.B, newImpl bool) {
	for _, n := range []int{16, 4096, 65536} {
		b.Run(map[bool]string{true: "new", false: "old"}[newImpl]+"/"+strconv.Itoa(n), func(b *testing.B) {
			payload := encodeFloat32PackedOld(nil, benchFloats(n))
			b.SetBytes(int64(len(payload)))
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				var dst []float32
				if newImpl {
					_ = decodeFloat32PackedNew(&dst, payload)
				} else {
					_ = decodeFloat32PackedOld(&dst, payload)
				}
			}
		})
	}
}

func BenchmarkUnmarshalFloat32Old(b *testing.B) { benchmarkUnmarshal32(b, false) }
func BenchmarkUnmarshalFloat32New(b *testing.B) { benchmarkUnmarshal32(b, true) }
