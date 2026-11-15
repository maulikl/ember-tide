import asyncio, sys, random
from pathlib import Path
sys.path.insert(0, "tests")
from lab import Lab, Sleeper, Wearable, ts

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)

async def main():
    sl = {"left": Sleeper(pref_c={"*": -0.2, "deep": -0.4}, watch=False, tap_prob=0.12, flash_phase="late", flash_minute=25,
                          wearables=[Wearable("Pixel Watch", skin=True, hr=True, every_s=240)]),
          "right": Sleeper(pref_c={"*": 0.3}, restless=0.03, tap_prob=0.08,
                           wearables=[Wearable("Oura Ring", skin=True, hr=True, every_s=300)])}
    lab = Lab(OUT, "2026-10-30T20:00", sleepers=sl, room_c=20.5, humidity=48)
    lab.p1.name, lab.p2.name = "Alex", "Sam"
    await lab.nights(10)
    # Watch/ring stages follow the bed's own night (in bed -> got up), so the Sleep page's
    # "Bed and watch" box agrees the way a real watch would.
    from datetime import date
    from embertide.engine.summary import summarize
    from lab import TZ
    rnd = random.Random(5)
    cyc = ["light","deep","deep","light","rem","light","deep","light","rem","light","awake","light","rem","light","rem","light"]
    w = {"deep": 27, "rem": 19, "light": 30, "awake": 6}
    for person, label, src in ((lab.p1, "Pixel Watch · Health Connect", "health-connect"), (lab.p2, "Oura Ring · Oura", "oura")):
        for d in range(6, 10):
            s = summarize(lab.store, person, date(2026, 11, d), TZ, lab.clock.now_ts(), raw=True)
            if not s or not s.get("in_bed_at") or not s.get("out_at"):
                continue
            # like a real watch: the session opens a couple of minutes after getting into bed, awake until asleep
            # (the bed's estimate when it's plausible, else ~20 min), and closes a few minutes before getting up
            opened = s["in_bed_at"] + rnd.randint(1, 4) * 60
            fell = s.get("fell_asleep_est")
            start = fell if fell and opened + 300 < fell < s["in_bed_at"] + 3600 else s["in_bed_at"] + rnd.randint(15, 25) * 60
            end = s["out_at"] - rnd.randint(3, 9) * 60
            mins = [w[st] * rnd.uniform(0.7, 1.3) for st in cyc]
            k = (end - start) / (sum(mins) * 60)            # stretch the cycle to fill the night
            segs, t = [{"start": opened, "end": start, "stage": "awake"}], start
            for st, m in zip(cyc, mins):
                segs.append({"start": t, "end": t + m * 60 * k, "stage": st}); t += m * 60 * k
            segs[-1]["end"] = end
            lab.store.put_stages(person.id, src, label, segs, opened - 3600, end + 3600)
    lab.store.put("household", lab.hh.model_dump())
    print("end", lab.clock.now().isoformat())

asyncio.run(main())
