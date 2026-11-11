from __future__ import annotations


STOP_ERROR_TIER3_VALIDATION_ACTIVE = "tier3_validation_workers_active"
STOP_ERROR_CORE_PROCESS_ACTIVE = "mihomo_process_active"
STOP_ERROR_CORE_OUTPUT_READER_ACTIVE = "mihomo_output_reader_active"
STOP_ERROR_RUNTIME_MONITOR_ACTIVE = "runtime_monitor_active"
STOP_ERROR_RUNTIME_BACKGROUND_ACTIVE = "runtime_background_workers_active"
STOP_ERROR_EXIT_QUALITY_REFRESH_ACTIVE = "exit_quality_refresh_active"


STOP_ERROR_MESSAGE_KEYS: dict[str, str] = {
    STOP_ERROR_TIER3_VALIDATION_ACTIVE: "备用节点验证任务未能在停止期限内结束",
    STOP_ERROR_CORE_PROCESS_ACTIVE: "Mihomo 核心进程未能在停止期限内退出",
    STOP_ERROR_CORE_OUTPUT_READER_ACTIVE: "Mihomo 输出读取线程未能在停止期限内结束",
    STOP_ERROR_RUNTIME_MONITOR_ACTIVE: "运行监控线程未能在停止期限内结束",
    STOP_ERROR_RUNTIME_BACKGROUND_ACTIVE: "运行后台任务未能在停止期限内结束",
    STOP_ERROR_EXIT_QUALITY_REFRESH_ACTIVE: "出口质量数据刷新未能在停止期限内结束",
}

STOP_ERROR_CORE_PROCESS_WITH_PID_MESSAGE_KEY = (
    "Mihomo 核心进程（PID {pid}）未能在停止期限内退出"
)
