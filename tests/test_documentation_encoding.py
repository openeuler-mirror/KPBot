"""Publication documentation must remain readable after cross-shell edits."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DocumentationEncodingTests(unittest.TestCase):
    def test_readme_and_docs_are_utf8_without_replacement_artifacts(self):
        paths = [ROOT / 'README.md', *sorted((ROOT / 'docs').rglob('*.md'))]
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text(encoding='utf-8')
                self.assertNotIn('\ufffd', content, 'Unicode replacement character in documentation')
                self.assertIsNone(re.search(r'\?{2,}', content), 'Repeated literal question marks may indicate lost text')

    def test_installation_readme_preserves_chinese_meaning(self):
        content = (ROOT / 'README.md').read_text(encoding='utf-8')
        for fragment in ('从源码安装时，Linux 和 macOS 使用', 'Codex 指南',
                         '目标 AI 编码工具（Codex 使用 Bash/Python 入口）',
                         '| `install_path` | 任意路径 | 当前目录（项目级） |',
                         '`$env:DSH_HOME/skills/` 或 `~/.dsh/skills/`',
                         'PowerShell 入口在设置了 `XDG_CONFIG_HOME` 时'):
            self.assertIn(fragment, content)


if __name__ == '__main__':
    unittest.main()
