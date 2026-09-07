from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from sign_service import run_all_scheduled_sign
from models import get_all_sign_times

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()

def scheduled_sign():
    run_all_scheduled_sign()

def start_scheduler():
    """启动调度器，加载数据库中所有启用的签到时间点"""
    global scheduler
    if not scheduler.running:
        scheduler = BackgroundScheduler()

    sign_times = get_all_sign_times()
    enabled_times = [t for t in sign_times if t.get('enabled', 1) == 1]

    if not enabled_times:
        logger.warning("未配置任何启用的签到时间，调度器未注册任务")
        scheduler.start()
        return

    for time_item in enabled_times:
        time_str = time_item['time_str']
        try:
            hour, minute = time_str.split(':')
            job_id = f'daily_sign_{time_item["id"]}'
            scheduler.add_job(
                scheduled_sign,
                trigger=CronTrigger(hour=int(hour), minute=int(minute)),
                id=job_id,
                replace_existing=True
            )
            logger.info(f"已注册定时签到任务: 每日 {time_str} (ID: {job_id})")
        except Exception as e:
            logger.error(f"注册时间点 {time_str} 失败: {e}")

    scheduler.start()
    logger.info(f"调度器已启动，共注册 {len(enabled_times)} 个签到时间点")

def stop_scheduler():
    global scheduler
    if scheduler.running:
        scheduler.shutdown(wait=False)
    scheduler = BackgroundScheduler()
    logger.info("调度器已停止")

def restart_scheduler():
    """重启调度器，时间配置修改后调用刷新任务"""
    stop_scheduler()
    start_scheduler()
