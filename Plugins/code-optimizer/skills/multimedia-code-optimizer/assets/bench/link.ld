ENTRY(_start)
SECTIONS
{
  . = 0x40000000;
  .text   : { *(.text.boot) *(.text*) }
  .rodata : { *(.rodata*) }
  .data   : { *(.data*) }
  . = ALIGN(16);
  __bss_start = .;
  .bss    : { *(.bss*) *(COMMON) }
  /* 1 GiB block page table, must be 4 KiB aligned and zeroed before use */
  . = ALIGN(4096);
  _l1_table = .;
  . = . + 4096;
  __bss_end = .;
  . = ALIGN(16);
  . = . + 0x100000;            /* 1 MiB stack */
  _stack_top = .;
}
