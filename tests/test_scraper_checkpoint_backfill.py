from __future__ import annotations

import datetime as dt

from UFC_Elf import scrape_ufc_fights as scraper


def _event(event_id: str, day_offset: int) -> scraper.EventMeta:
    return scraper.EventMeta(
        event_id=event_id,
        event_url=f"https://example.com/event/{event_id}",
        event_name=f"Event {event_id}",
        event_date=dt.date(2024, 1, 1) + dt.timedelta(days=day_offset),
        event_city="Las Vegas",
        event_state="NV",
        event_country="USA",
    )


def _stub(fight_id: str) -> scraper.FightStub:
    return scraper.FightStub(
        fight_id=fight_id,
        fight_url=f"https://example.com/fight/{fight_id}",
        bout_index=1,
        fighter_1_id="f1",
        fighter_1_name="Fighter 1",
        fighter_1_url="https://example.com/fighter/f1",
        fighter_2_id="f2",
        fighter_2_name="Fighter 2",
        fighter_2_url="https://example.com/fighter/f2",
        fighter_1_status="W",
        fighter_2_status="L",
        weight_class="Lightweight",
        method="U-DEC",
        round_ended=3,
        time_ended="5:00",
        kd_1=0,
        kd_2=0,
        sig_str_1_landed=0,
        sig_str_1_attempted=0,
        sig_str_2_landed=0,
        sig_str_2_attempted=0,
        td_1_landed=0,
        td_1_attempted=0,
        td_2_landed=0,
        td_2_attempted=0,
        sub_1=0,
        sub_2=0,
        ctrl_seconds_1=0,
        ctrl_seconds_2=0,
    )


def test_checkpoint_backfill_marks_fully_complete_events(tmp_path, monkeypatch):
    store = scraper.CheckpointStore(tmp_path / "checkpoint.sqlite")
    try:
        store.insert_fight({"fight_id": "a1", "event_id": "e1"})
        store.insert_fight({"fight_id": "a2", "event_id": "e1"})
        store.insert_fight({"fight_id": "b1", "event_id": "e2"})
        store.commit()

        events = [_event("e1", 0), _event("e2", 1), _event("e3", 2)]
        fight_map = {"e1": ["a1", "a2"], "e2": ["b1"], "e3": ["c1"]}

        def fake_parse_event_fights(_client, event):
            return [_stub(fid) for fid in fight_map[event.event_id]]

        monkeypatch.setattr(scraper, "parse_event_fights", fake_parse_event_fights)

        processed_event_ids: set[str] = set()
        backfilled = scraper._backfill_processed_events_from_existing_fights(
            store=store,
            client=object(),
            events=events,
            processed_event_ids=processed_event_ids,
        )

        assert backfilled == 2
        assert processed_event_ids == {"e1", "e2"}
        assert store.processed_event_ids() == {"e1", "e2"}
    finally:
        store.close()


def test_checkpoint_backfill_stops_on_partial_event(tmp_path, monkeypatch):
    store = scraper.CheckpointStore(tmp_path / "checkpoint.sqlite")
    try:
        store.insert_fight({"fight_id": "a1", "event_id": "e1"})
        store.insert_fight({"fight_id": "b1", "event_id": "e2"})
        store.commit()

        events = [_event("e1", 0), _event("e2", 1)]
        fight_map = {"e1": ["a1", "a2"], "e2": ["b1"]}

        def fake_parse_event_fights(_client, event):
            return [_stub(fid) for fid in fight_map[event.event_id]]

        monkeypatch.setattr(scraper, "parse_event_fights", fake_parse_event_fights)

        processed_event_ids: set[str] = set()
        backfilled = scraper._backfill_processed_events_from_existing_fights(
            store=store,
            client=object(),
            events=events,
            processed_event_ids=processed_event_ids,
        )

        assert backfilled == 0
        assert processed_event_ids == set()
        assert store.processed_event_ids() == set()
    finally:
        store.close()
