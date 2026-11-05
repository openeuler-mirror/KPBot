# Mini Standard MS-1.0 —— MiniGEMM

> 用于「标准符合性测试框架搭建」用例的最小条款样本。
> 条款文本即判定依据（verbatim），符合性测试的每条断言都必须绑定其中一条。

## MS-1 §1.1 Quick return

"This routine returns immediately if alpha is equal to zero and beta is equal to one."

## MS-1 §1.2 Empty sum

"If k is zero the summation over k is empty, and C := beta * C."

## MS-1 §1.3 Input immutability

"The routine shall not modify the contents of A or B."

## MS-1 §1.4 Zero dimension

"If m or n is zero the routine returns immediately and C is unchanged."
