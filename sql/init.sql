-- HomeMind 数据库初始化脚本
-- 对应 SRS 5.1 核心数据实体。V0.5 记忆系统启用时由 MySQL 容器自动执行。
-- V0.1 阶段数据仍在内存，此脚本为后续版本预留。

CREATE DATABASE IF NOT EXISTS homemind DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE homemind;

-- 家庭（多租户隔离，FR-SEC-003）
CREATE TABLE IF NOT EXISTS family (
    family_id    VARCHAR(36) PRIMARY KEY,
    name         VARCHAR(64) NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 家庭成员（权限分级，FR-MEM-008）
-- role: L4 管理员 / L3 成人 / L2 儿童 / L1 访客
CREATE TABLE IF NOT EXISTS member (
    member_id    VARCHAR(36) PRIMARY KEY,
    family_id    VARCHAR(36) NOT NULL,
    name         VARCHAR(64) NOT NULL,
    role         VARCHAR(8) NOT NULL DEFAULT 'L3',
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_family (family_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 设备
CREATE TABLE IF NOT EXISTS device (
    device_id    VARCHAR(64) PRIMARY KEY,
    family_id    VARCHAR(36) NOT NULL,
    room         VARCHAR(32) NOT NULL,
    name         VARCHAR(64) NOT NULL,
    device_type  VARCHAR(32) NOT NULL,
    power        TINYINT(1) NOT NULL DEFAULT 0,
    value        DECIMAL(6,2) DEFAULT NULL,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_family_room (family_id, room)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 长期结构化记忆（FR-MEM-002）
-- memory_type: preference 偏好 / member_profile 成员画像 / device_habit 设备习惯 / scene 场景
--
-- 唯一键必须是 (family_id, member_id, memory_key) 三元组：
-- 两个家庭各有一条"preference:空调温度"（深圳 26 度、青岛 20 度），
-- key 完全一样。只按 (member_id, memory_key) 唯一，后写的会把先写的覆盖掉——
-- 你在深圳调好的空调温度，会被小明家的设置冲掉。
-- 这一列在 SQLite 版里靠 memory_struct_v2 迁移补上，两边结构必须一致。
CREATE TABLE IF NOT EXISTS memory_struct (
    memory_id    BIGINT AUTO_INCREMENT PRIMARY KEY,
    family_id    VARCHAR(36) NOT NULL DEFAULT 'family_sz',
    member_id    VARCHAR(36) NOT NULL,
    memory_type  VARCHAR(32) NOT NULL,
    memory_key   VARCHAR(128) NOT NULL,
    memory_value TEXT NOT NULL,
    version      INT NOT NULL DEFAULT 1,
    importance   DECIMAL(3,2) NOT NULL DEFAULT 0.50,
    visible_role VARCHAR(8) NOT NULL DEFAULT 'L3',
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_family_member_key (family_id, member_id, memory_key),
    INDEX idx_family (family_id),
    INDEX idx_member_type (member_id, memory_type),
    INDEX idx_key (memory_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 记忆变更历史（FR-MEM-006 冲突更新留痕）
CREATE TABLE IF NOT EXISTS memory_history (
    history_id   BIGINT AUTO_INCREMENT PRIMARY KEY,
    memory_id    BIGINT NOT NULL,
    old_value    TEXT,
    new_value    TEXT NOT NULL,
    changed_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_memory (memory_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 日程 / 提醒（FR-TOOL-002）
CREATE TABLE IF NOT EXISTS schedule (
    schedule_id  VARCHAR(36) PRIMARY KEY,
    member_id    VARCHAR(36) NOT NULL,
    content      VARCHAR(512) NOT NULL,
    scheduled_at DATETIME NOT NULL,
    idempotency_key VARCHAR(128) DEFAULT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_idempotency (idempotency_key),
    INDEX idx_member_time (member_id, scheduled_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 工具调用审计（FR-SEC-004）
CREATE TABLE IF NOT EXISTS tool_call_log (
    log_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    trace_id     VARCHAR(32) NOT NULL,
    member_id    VARCHAR(36) NOT NULL,
    tool_name    VARCHAR(64) NOT NULL,
    arguments    JSON,
    success      TINYINT(1) NOT NULL,
    error        VARCHAR(512) DEFAULT NULL,
    duration_ms  DECIMAL(10,2) DEFAULT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_trace (trace_id),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
