	.text
	.globl _ZN6kernel11microkernel7fma_4x417h0123456789abcdefE
	.p2align 2
_ZN6kernel11microkernel7fma_4x417h0123456789abcdefE:
	stp x29, x30, [sp, #-16]!
	fmla v0, v0, v1
	fmla v2, v2, v3
	fmla v4, v4, v5
	fmla v6, v6, v7
	ldr x9, [sp, #8]
	ret
_ZN6kernel11microkernel12fma_4x4_safe17hfedcba9876543210E:
	ret
