#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REQUIRED_HEADINGS = [
  "# 测试方案",
  "## 功能边界",
  "### 验证范围",
  "### 不验证范围",
  "### 代码定位",
  "## 触发入口",
  "### 触发入口结论",
  "## 依赖服务",
  "### 应用服务",
  "### Docker 依赖",
  "## 数据准备",
  "### 种子数据扩展",
  "## Mock 策略",
  "## 测试类型选择",
  "## 覆盖等级",
  "## 不可替代断言",
  "## 降级路径",
  "## 测试用例",
  "## 执行进度",
  "## 断言设计",
  "### HTTP 断言",
  "### 数据库断言",
  "### Redis / MQ / Mock 断言",
  "### 日志与监控断言",
];

const REQUIRED_TOKENS = [
  "推荐入口",
  "不推荐入口",
  "基线变更",
  "判断依据",
  "NNA-001",
  "TC-001",
  "方案校验通过",
  "目标回归通过",
  "全量回归通过",
];

const FORBIDDEN_GENERATED_ARTIFACT_PATHS = [
  "harness/tests/api/",
  "harness\\tests\\api\\",
  "harness/tests/fixtures/http/",
  "harness\\tests\\fixtures\\http\\",
  "harness/tests/fixtures/db/tmp/",
  "harness\\tests\\fixtures\\db\\tmp\\",
  "harness/docs/",
  "harness\\docs\\",
];

const ALLOWED_PRIMARY = ["node_api", "java_unit", "java_integration", "manual_smoke"];
const ALLOWED_COVERAGE_LEVELS = ["unit", "service", "integration", "e2e"];
const TEMP_SQL_NAME_PATTERN = /^TC-[^\s/\\`|]+-[^\s/\\`|]+\.sql$/;

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function headingPositions(text) {
  const positions = new Map();

  for (const heading of REQUIRED_HEADINGS) {
    let match;
    if (heading.startsWith("# ") && !heading.startsWith("## ")) {
      const keyword = heading.replace(/^#\s+/, "").trim();
      match = new RegExp(`^#\\s+.*${escapeRegExp(keyword)}.*$`, "m").exec(text);
    } else {
      match = new RegExp(`^${escapeRegExp(heading)}\\s*$`, "m").exec(text);
    }

    if (match) {
      positions.set(heading, match.index);
    }
  }

  return positions;
}

export function validate(text) {
  const errors = [];
  const positions = headingPositions(text);

  for (const heading of REQUIRED_HEADINGS) {
    if (!positions.has(heading)) {
      errors.push(`missing heading: ${heading}`);
    }
  }

  if (errors.length === 0) {
    let lastPosition = -1;
    for (const heading of REQUIRED_HEADINGS) {
      const position = positions.get(heading);
      if (position <= lastPosition) {
        errors.push(`heading out of order: ${heading}`);
      }
      lastPosition = position;
    }
  }

  for (const token of REQUIRED_TOKENS) {
    if (!text.includes(token)) {
      errors.push(`missing required token: ${token}`);
    }
  }

  for (const forbiddenPath of FORBIDDEN_GENERATED_ARTIFACT_PATHS) {
    if (text.includes(forbiddenPath)) {
      errors.push(
        `generated test scripts, fixtures, and docs must be under <featureRoot>/test/, not ${forbiddenPath}`,
      );
    }
  }

  const tempSqlPathPattern = /(?:fixtures\/db\/tmp\/|fixtures\\db\\tmp\\)([^\s`|)]+\.sql)/g;
  for (const match of text.matchAll(tempSqlPathPattern)) {
    const filename = match[1];
    if (!TEMP_SQL_NAME_PATTERN.test(filename)) {
      errors.push(
        `temporary SQL files under fixtures/db/tmp must be named TC-{需求名称}-xxx.sql, got ${filename}`,
      );
    }
  }

  const primaryPattern = ALLOWED_PRIMARY.join("|");
  const primaryMatch =
    new RegExp(`\\*\\*\\\`?(${primaryPattern})\\\`?\\*\\*`).exec(text) ||
    new RegExp(`\\\`(${primaryPattern})\\\`.*主`).exec(text);
  if (!primaryMatch) {
    errors.push("missing primary test type (e.g. **node_api**（主）)");
  }

  const coveragePattern = ALLOWED_COVERAGE_LEVELS.join("|");
  for (const key of ["目标覆盖等级", "最低可接受等级"]) {
    const match = new RegExp(`\\|\\s*${escapeRegExp(key)}\\s*\\|\\s*\\\`?(${coveragePattern})\\\`?`).exec(text);
    if (!match) {
      errors.push(`missing coverage level: ${key}`);
    }
  }

  const approvalMatch = /降级是否需用户批准\s*\|\s*\*\*(是|true)\*\*/.exec(text);
  if (!approvalMatch) {
    errors.push("missing or invalid: 降级是否需用户批准 must be **是**");
  }

  for (const item of ["方案校验通过", "目标回归通过", "全量回归通过"]) {
    const progressPattern = new RegExp(`^-\\s+\\[[ xX]\\]\\s+${escapeRegExp(item)}\\s*$`, "m");
    if (!progressPattern.test(text)) {
      errors.push(`missing execution progress item: ${item}`);
    }
  }

  return errors;
}

function fail(message) {
  console.error(`ERROR: ${message}`);
  return 1;
}

function main() {
  if (process.argv.length !== 3) {
    return fail("usage: validate-test-plan.mjs <path-to-test-plan.md>");
  }

  const path = process.argv[2];
  let text;
  try {
    text = fs.readFileSync(path, "utf8").replace(/^\uFEFF/, "");
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return fail(`file not found: ${path}`);
    }
    throw error;
  }

  const errors = validate(text);
  if (errors.length > 0) {
    for (const error of errors) {
      console.error(`ERROR: ${error}`);
    }
    return 1;
  }

  console.log("test plan is valid");
  return 0;
}

if (process.argv[1] && path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1])) {
  process.exitCode = main();
}
