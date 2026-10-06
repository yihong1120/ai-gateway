"""Source-scoped replay protection; display wording is not event identity.

A changed schedule or colliding slots requires reconciliation, not a new write.
Legacy receipts are looked up through owned CSVs, never inferred from titles.
"""
from __future__ import annotations

from datetime import datetime

from . import calendar_sync
from .calendar_projects import normalize_project_name
from .calendar_projects import project_name_from_description
from official_document_workflow.infrastructure.eip import calendar_csv


def event_slot(row):
    return [
        datetime.strptime(row['Start Date'], '%Y/%m/%d').date().isoformat(), calendar_csv.canonical_time(row, 'Start'),
        datetime.strptime(row['End Date'], '%Y/%m/%d').date().isoformat(),
        calendar_csv.canonical_time(row, 'End'), row['All day event'],
        normalize_project_name(project_name_from_description(row['Description'])),
    ]


async def prepare_source_replay(events, user_id, journal, stage, read_csv):

    scopes = {}
    for event in events:
        origin = event['row'].get('_eip_origin')
        if origin:
            scopes.setdefault(origin['scope'], []).append(event)
    for scope, group in scopes.items():
        slot_values = [event_slot(event['row']) for event in group]
        slots = [journal.key(user_id, {'source': scope, 'slot': value}) for value in slot_values]
        slot_set = set(slots)
        if len(slot_set) != len(slots):
            raise ValueError('同一來源含相同時段與工程的多筆候選，須先核對是否為不同工作；未重複匯入。')
        batch = journal.key(user_id, {'source_batch': scope})
        manifest = journal.key(user_id, {'slots': sorted(slots)})
        previous_manifest = journal.source_manifest(batch)
        if previous_manifest is not None and previous_manifest != manifest:
            raise ValueError('此來源已有匯入紀錄，但事件數量、時間或工程已變更；須核對原行程，未另建重複行程。')
        origin = group[0]['row']['_eip_origin']
        old_by_slot = {}
        legacy_slots = {}
        for key, value in zip(slots, slot_values):
            legacy_slots.setdefault(tuple(value[:-1]), []).append(key)
        for file_id in origin['prior_csv_ids']:
            rows, payload = await read_csv(
                origin['owner'], file_id,
                default_duration_minutes=origin['duration'], include_history=False,
            )
            prior_events = calendar_sync.verified_events(rows, await stage(payload), user_id, journal)
            prior_states = journal.statuses(k for event in prior_events for k in event['aliases'])
            for prior in prior_events:
                known = [k for k in prior['aliases'] if k in prior_states]
                if not known:
                    continue
                prior_slot = event_slot(prior['row'])
                slot = journal.key(user_id, {'source': scope, 'slot': prior_slot})
                if slot not in slot_set and not prior_slot[-1]:
                    # Older source-owned CSVs did not carry a project heading.
                    # Only a unique identical interval in this same source batch
                    # can inherit their receipt; never guess between projects.
                    matches = legacy_slots.get(tuple(prior_slot[:-1]), [])
                    if len(matches) == 1:
                        slot = matches[0]
                if slot not in slot_set:
                    raise ValueError('舊 CSV 已有匯入紀錄且時程不一致；須核對原行程，未重送。')
                old_by_slot.setdefault(slot, []).extend(known)
        states = journal.statuses([
            *slots, *(k for event in group for k in event['aliases']),
            *(k for keys in old_by_slot.values() for k in keys),
        ])
        for event, slot in zip(group, slots):
            event['aliases'] = list(dict.fromkeys((*event['aliases'], slot, *old_by_slot.get(slot, ()))))
            known = [k for k in event['aliases'] if k in states]
            # Unknown always dominates done, including a legacy in-flight write.
            event['key'] = next(
                (k for k in known if states[k] == 'unknown'),
                known[0] if known else slot,
            )
            event['source_batch'] = (batch, manifest)
    return events
