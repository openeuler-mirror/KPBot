	.text
	.globl dot_f32
	.p2align 2
dot_f32:
	fmul s0, s1, s2
	fadd s0, s0, s2
	ret
	.cfi_endproc
