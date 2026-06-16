"""APScheduler-based scheduling of playlist playback and CEC display actions."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, cec

# 0=Monday .. 6=Sunday  (matches APScheduler day_of_week numbering)
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Scheduler:
    def __init__(self, engine, log=print):
        self.engine = engine
        self.log = log
        self.sched = BackgroundScheduler()
        self.sched.start()

    def _trigger(self, sch):
        hh, mm = (sch.get("time") or "00:00").split(":")
        days = sch.get("days") or list(range(7))
        dow = ",".join(DAY_NAMES[d] for d in days if 0 <= d < 7) or "*"
        return CronTrigger(day_of_week=dow, hour=int(hh), minute=int(mm))

    def _make_job(self, sch):
        kind = sch.get("kind")
        if kind == "play_playlist":
            pid = sch.get("playlist_id")

            def job():
                cfg = config.load()
                pl = config.get_playlist(cfg, pid)
                if pl:
                    self.log("schedule: play playlist %s" % pl.get("name"))
                    self.engine.play_playlist(pl)
                else:
                    self.log("schedule: playlist %s not found" % pid)
            return job
        if kind == "stop":
            def job():
                self.log("schedule: stop -> default")
                self.engine.stop()
            return job
        if kind == "cec":
            action = sch.get("cec_action", "on")

            def job():
                r = cec.run_action(action)
                self.log("schedule: cec %s -> %s" % (action, "ok" if r.get("ok") else r.get("output")))
            return job
        return None

    def reload(self):
        self.sched.remove_all_jobs()
        cfg = config.load()
        for sch in cfg.get("schedules", []):
            if not sch.get("enabled", True):
                continue
            job = self._make_job(sch)
            if not job:
                continue
            try:
                self.sched.add_job(job, self._trigger(sch), id=sch["id"],
                                   replace_existing=True, misfire_grace_time=60)
            except Exception as e:  # noqa
                self.log("failed to schedule %s: %s" % (sch.get("id"), e))
        self.log("scheduler reloaded: %d job(s)" % len(self.sched.get_jobs()))

    def next_runs(self):
        out = {}
        for j in self.sched.get_jobs():
            out[j.id] = j.next_run_time.isoformat() if j.next_run_time else None
        return out
