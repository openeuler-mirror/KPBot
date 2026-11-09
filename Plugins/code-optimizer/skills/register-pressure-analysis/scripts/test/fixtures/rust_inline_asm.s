	.text
	.globl _ZN6kernel11microkernel7fma_4x417h0123456789abcdefE
_ZN6kernel11microkernel7fma_4x417h0123456789abcdefE:
	# APP
	fmla v0, v0, v1
	fmla v2, v2, v3
	# NO_APP
	ret
