# ${stage_name}

## 任务
准备项目环境，建立性能基线。

## 你的角色
project preparation expert

## 上下文
用户优化目标：
- 优化类型：${context.user_choice}
- 项目路径：${context.project_path}
- 代码路径：${context.code_path}
- 函数名：${context.function_name}
- 测试用例：${context.test_cases}
- 测试方法：${context.test_method}

## 执行
使用 Skill tool，skill 名称为 `prepare-project`
参数：
- project_path：${context.project_path}
- target：${context.function_name}
- test_cases：${context.test_cases}
- test_method：${context.test_method}

## 输出格式
返回 JSON 契约：
```json
{
  "repo": {
    "path": "<project_path>",
    "vcs": "git|none",
    "build_system": "cmake|make|meson|bazel|autotools|unknown",
    "compiler": "<compiler version or null>",
    "test_framework": "googletest|catch2|unity|none",
    "compilation": {
      "cflags": "<CFLAGS value or null>",
      "cxxflags": "<CXXFLAGS value or null>",
      "ldflags": "<LDFLAGS value or null>",
      "build_type": "Release|Debug|RelWithDebInfo|MinSizeRel|unknown",
      "flag_sources": [
        {
          "file": "CMakeLists.txt",
          "variable": "CMAKE_C_FLAGS",
          "value": "-O2 -g"
        }
      ],
      "performance_flags": {
        "optimization_level": "-O0|-O1|-O2|-O3|-Os|unknown",
        "arch_flags": ["-march=armv8-a"],
        "cpu_flags": ["-mcpu=tsv110"],
        "math_flags": [],
        "lto_enabled": false,
        "pgo_enabled": false,
        "auto_vectorization": "enabled|disabled|unknown"
      }
    }
  },
  "machine": {
    "arch": "aarch64|x86_64|unknown",
    "cpu_model": "<CPU model name>",
    "isa_features": {
      "simd": ["neon", "sve", "avx2"],
      "architecture_extensions": ["crc32", "crypto"]
    },
    "cache_info": {
      "l1d_size_kb": 64,
      "cache_line_size": 64
    },
    "os": "Linux 5.14.0-284.11.1.el9_2.aarch64",
    "platform_match": "true|false|partial_match",
    "platform_note": null
  },
  "target": {
    "module": "<module name or null>",
    "source_files": ["<file1>", "<file2>"],
    "entry_functions": ["<func1>", "<func2>"],
    "call_chains": {}
  },
  "baseline": {
    "build_ok": true,
    "tests_pass": true,
    "metrics": {}
  },
  "warnings": [],
  "status": "ready" | "blocked" | "partial"
}
```

## 引用 Skill 内容
详见 `skills/prepare-project/SKILL.md`
