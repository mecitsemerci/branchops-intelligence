from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from branchops.application.interfaces.source_adapter import (
    SourceAuthError,
    SourcePayloadError,
    UnknownSourceError,
    event_envelope,
)
from branchops.config.settings import Settings, SourceAuthSettings, SourceSettings
from branchops.domain.entities.incident import IncidentComment
from branchops.domain.enums import IncidentEventType
from branchops.infrastructure.sources import build_source_registry
from branchops.infrastructure.sources.csv_import.adapter import CsvImportAdapter, fold_header
from branchops.infrastructure.sources.servicenow.adapter import ServiceNowWebhookAdapter
from tests.helpers import ROOT

SNOW_SAMPLES = ROOT / "samples" / "servicenow"
CSV_SAMPLES = ROOT / "samples" / "csv"
SNOW_SECRETS_ENV = "SNOW_TEST_SECRETS"
IST = ZoneInfo("Europe/Istanbul")
CSV_MAPPING = {
    "summary": "Konu",
    "description": "Açıklama",
    "branch_code": "Sube Kodu",
    "application": "Uygulama",
    "status": "Durum",
    "priority": "Öncelik",
    "reporter": "Bildiren",
    "created_at": "Oluşturma",
    "updated_at": "Güncelleme",
    "group_key": "Ekip",
}
IMPORTED_AT = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _mask(text: str) -> str:
    return text.replace("1234", "****")


# -- ServiceNow -----------------------------------------------------------------------------------------


def snow_sample(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SNOW_SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def snow_adapter() -> ServiceNowWebhookAdapter:
    return ServiceNowWebhookAdapter(
        SourceSettings(
            id="snow",
            type="servicenow_webhook",
            auth=SourceAuthSettings(mode="token", secrets_env=SNOW_SECRETS_ENV),
            field_mapping={"branch_code": "u_branch_code", "application": "cmdb_ci"},
        )
    )


class TestServiceNow:
    def test_insert(self) -> None:
        ev = snow_adapter().normalize(snow_sample("incident_insert"))
        assert ev.source == "snow"
        assert ev.event_type is IncidentEventType.RECORD_CREATED
        assert ev.raw_event_name == "servicenow:incident.insert"
        assert ev.external_key == "INC0012345"
        assert ev.incident_key == "snow:INC0012345"
        assert ev.external_id == "9d385017c611228701d22104cc95c371"
        assert ev.group_key == "Şube Uygulamaları Destek"
        assert ev.fields["summary"].startswith("Gişe Ekranı uygulamasında EFT")
        assert "EFT-4012" in ev.fields["description"]
        assert ev.fields["status"] == "Yeni"
        assert ev.fields["priority"] == "2 - Yüksek"
        assert ev.fields["reporter"] == "Şube Personeli"
        assert ev.fields["components"] == ["Yazılım", "Para Transferi"]
        assert ev.fields["created_at"] == datetime(2026, 10, 15, 6, 5, 10, tzinfo=UTC)
        assert "comments" not in ev.fields  # empty journal values are not entries
        assert ev.mapped_fields == {"branch_code": "1234", "application": "Gişe Ekranı"}
        assert ev.source_updated_at == datetime(2026, 10, 15, 6, 5, 10, tzinfo=UTC)
        assert ev.event_timestamp == ev.source_updated_at
        assert ev.actor == "sube.personeli"

    def test_update_with_journal_comment_has_stable_id(self) -> None:
        adapter = snow_adapter()
        first = adapter.normalize(snow_sample("incident_update_comment"))
        second = adapter.normalize(snow_sample("incident_update_comment"))
        assert first.event_type is IncidentEventType.RECORD_UPDATED
        comments = first.fields["comments"]
        assert len(comments) == 1
        comment = comments[0]
        assert isinstance(comment, IncidentComment)
        assert comment.comment_id == second.fields["comments"][0].comment_id
        assert comment.comment_id.startswith("comments-")
        assert comment.author == "ayse.yilmaz"
        assert comment.created_at == datetime(2026, 10, 15, 9, 12, 44, tzinfo=UTC)
        assert "EFT-4012" in comment.body
        assert first.fields["status"] == "Çalışılıyor"
        assert first.changed_fields == ["status"]

    def test_work_note_and_comment_get_distinct_ids(self) -> None:
        payload = snow_sample("incident_update_comment")
        payload["work_notes"] = "Uygulama sunucusu loglarında zaman aşımı görüldü."
        ids = {c.comment_id for c in snow_adapter().normalize(payload).fields["comments"]}
        assert len(ids) == 2

    def test_partial_update_only_contains_present_attributes(self) -> None:
        ev = snow_adapter().normalize(snow_sample("incident_update_partial"))
        assert ev.event_type is IncidentEventType.RECORD_UPDATED
        assert set(ev.fields) == {"external_id", "status", "priority"}
        assert ev.fields["status"] == "Çözüldü"
        assert ev.mapped_fields == {}
        assert ev.changed_fields == ["status", "priority"]
        assert ev.group_key is None

    def test_delete(self) -> None:
        ev = snow_adapter().normalize(snow_sample("incident_delete"))
        assert ev.event_type is IncidentEventType.RECORD_DELETED
        assert ev.external_key == "INC0012345"
        assert ev.raw_event_name == "servicenow:incident.delete"

    def test_display_value_objects_and_present_but_empty_mapping(self) -> None:
        payload = snow_sample("incident_update_partial")
        payload["state"] = {"value": "6", "display_value": ""}
        payload["u_branch_code"] = {"value": "", "display_value": ""}
        payload["cmdb_ci"] = {"value": "abc", "display_value": "Kasa İşlemleri"}
        payload["changed_fields"] = ["u_branch_code"]
        ev = snow_adapter().normalize(payload)
        assert ev.fields["status"] == "6"  # falls back to value when display_value is empty
        assert ev.mapped_fields == {"branch_code": "", "application": "Kasa İşlemleri"}
        assert ev.changed_fields == ["branch_code"]

    def test_iso_timestamp_accepted(self) -> None:
        payload = snow_sample("incident_update_partial")
        payload["sys_updated_on"] = "2026-10-15T14:40:02+03:00"
        ev = snow_adapter().normalize(payload)
        assert ev.source_updated_at == datetime(2026, 10, 15, 11, 40, 2, tzinfo=UTC)

    def test_unsupported_table_and_event(self) -> None:
        payload = snow_sample("incident_insert")
        payload["table"] = "change_request"
        ev = snow_adapter().normalize(payload)
        assert ev.event_type is IncidentEventType.UNSUPPORTED
        assert ev.raw_event_name == "servicenow:change_request.insert"
        unknown = snow_adapter().normalize({"event": "merge", "number": "bad number"})
        assert unknown.event_type is IncidentEventType.UNSUPPORTED
        assert unknown.external_key is None

    @pytest.mark.parametrize("number", ["INC 0012345", "", None, "<script>"])
    def test_invalid_number_rejected(self, number: str | None) -> None:
        payload = snow_sample("incident_insert")
        payload["number"] = number
        with pytest.raises(SourcePayloadError):
            snow_adapter().normalize(payload)

    def test_non_dict_payload_rejected(self) -> None:
        with pytest.raises(SourcePayloadError):
            snow_adapter().normalize([1, 2])  # type: ignore[arg-type]

    def test_idempotency_key(self) -> None:
        adapter = snow_adapter()
        payload = snow_sample("incident_update_comment")
        key = adapter.idempotency_key(payload, {})
        assert key == "9d385017c611228701d22104cc95c371:2026-10-15 09:12:44:update"
        assert adapter.idempotency_key(dict(payload), {"X-Other": "1"}) == key
        no_ts = {k: v for k, v in payload.items() if k != "sys_updated_on"}
        fallback = adapter.idempotency_key(no_ts, {})
        assert fallback.startswith("sha256:")
        assert fallback == adapter.idempotency_key(dict(no_ts), {})

    def test_authenticate_token_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SNOW_SECRETS_ENV, "old-secret,new-secret")
        adapter = snow_adapter()
        mechanism = adapter.authenticate(
            body=b"{}", headers={"X-BranchOps-Webhook-Token": "new-secret"}, query={}, client_host="10.0.0.5"
        )
        assert mechanism == "token-header"
        with pytest.raises(SourceAuthError):
            adapter.authenticate(
                body=b"{}", headers={"X-BranchOps-Webhook-Token": "wrong"}, query={}, client_host=None
            )
        with pytest.raises(SourceAuthError):
            adapter.authenticate(body=b"{}", headers={}, query={}, client_host=None)

    def test_transform_applied_to_free_text(self) -> None:
        adapter = snow_adapter()
        ev = adapter.normalize(snow_sample("incident_insert"), transform=_mask)
        assert "1234" not in ev.fields["description"]
        assert "SB****-GISE03" in ev.fields["description"]
        assert ev.mapped_fields["branch_code"] == "****"
        payload = snow_sample("incident_update_comment")
        payload["comments"] = "Şube 1234 tekrar aradı."
        comment = adapter.normalize(payload, transform=_mask).fields["comments"][0]
        assert comment.body == "Şube **** tekrar aradı."


# -- CSV / XLSX / JSONL ---------------------------------------------------------------------------------


def csv_adapter(**options: Any) -> CsvImportAdapter:
    return CsvImportAdapter(
        SourceSettings(id="csv", type="csv_import", field_mapping=CSV_MAPPING, options=options)
    )


class TestCsvImport:
    def test_semicolon_csv_with_id_column(self) -> None:
        events = list(
            csv_adapter(id_column="Kayıt No").read_file(
                CSV_SAMPLES / "branch_incidents.csv", imported_at=IMPORTED_AT
            )
        )
        assert [e.external_key for e in events] == [f"IMP-100{n}" for n in range(1, 6)]
        first = events[0]
        assert first.source == "csv"
        assert first.event_type is IncidentEventType.RECORD_CREATED
        assert first.raw_event_name == "csv_import"
        assert first.fields["summary"] == "Gişe Ekranı EFT gönderiminde zaman aşımı"
        assert first.fields["status"] == "Açık"
        assert first.fields["reporter"] == "Şube Personeli"
        assert first.group_key == first.fields["group_key"] == "Şube Uygulamaları"
        assert first.mapped_fields == {"branch_code": "1234", "application": "Gişe Ekranı"}
        assert first.fields["created_at"] == datetime(2026, 10, 14, 9, 12, tzinfo=IST)
        assert first.source_updated_at == datetime(2026, 10, 14, 6, 30, tzinfo=UTC)
        assert first.event_timestamp == IMPORTED_AT
        assert first.actor is None

    def test_empty_cells_are_omitted(self) -> None:
        events = csv_adapter(id_column="Kayıt No").read_file(
            CSV_SAMPLES / "branch_incidents.csv", imported_at=IMPORTED_AT
        )
        by_key = {e.external_key: e for e in events}
        assert "branch_code" not in by_key["IMP-1003"].mapped_fields
        generic = by_key["IMP-1004"]
        assert "application" not in generic.mapped_fields
        assert generic.source_updated_at is None
        assert generic.fields["description"] == "Sistem çalışmıyor acil bakılsın."

    def test_known_id_becomes_update(self) -> None:
        events = csv_adapter(id_column="Kayıt No").read_file(
            CSV_SAMPLES / "branch_incidents.csv", is_known=lambda key: key == "IMP-1002"
        )
        types = {e.external_key: e.event_type for e in events}
        assert types["IMP-1002"] is IncidentEventType.RECORD_UPDATED
        assert types["IMP-1001"] is IncidentEventType.RECORD_CREATED

    def test_header_normalisation(self, tmp_path: Path) -> None:
        assert fold_header("  ŞUBE   KODU ") == fold_header("Sube Kodu") == "sube kodu"
        assert fold_header("ÖNCELİK") == fold_header("Öncelik") == "oncelik"
        path = tmp_path / "upper.csv"
        path.write_text("KAYIT NO,konu,ŞUBE KODU,ONCELIK\nX-1,Konu metni,0042,Yüksek\n", encoding="utf-8")
        (ev,) = csv_adapter(id_column="kayit no").read_file(path)
        assert ev.external_key == "X-1"
        assert ev.fields["summary"] == "Konu metni"
        assert ev.fields["priority"] == "Yüksek"
        assert ev.mapped_fields == {"branch_code": "0042"}
        assert ev.group_key == "upper"  # default batch = file stem

    def test_comma_and_tab_sniffing_and_labels(self, tmp_path: Path) -> None:
        comma = tmp_path / "comma.csv"
        comma.write_text('Konu,Etiketler\n"Hata, acil","eft; gise,kasa"\n', encoding="utf-8")
        adapter = CsvImportAdapter(
            SourceSettings(
                id="csv",
                type="csv_import",
                field_mapping={"summary": "Konu", "labels": "Etiketler"},
                options={"batch": "ekim-import"},
            )
        )
        (ev,) = adapter.read_file(comma)
        assert ev.fields["summary"] == "Hata, acil"
        assert ev.fields["labels"] == ["eft", "gise", "kasa"]
        assert ev.group_key == "ekim-import"
        tab = tmp_path / "tab.txt"
        tab.write_text("Konu\tEtiketler\nYavaşlık\ta\n", encoding="utf-8")
        (ev2,) = adapter.read_file(tab)
        assert ev2.fields == {"summary": "Yavaşlık", "labels": ["a"], "group_key": "ekim-import"}

    def test_invalid_id_raises_with_context(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.csv"
        path.write_text("Kayıt No;Konu\n<bad>;x\n", encoding="utf-8")
        with pytest.raises(SourcePayloadError, match=r"bad\.csv row 2"):
            list(csv_adapter(id_column="Kayıt No").read_file(path))

    def test_jsonl_hash_keys_are_stable_and_content_addressed(self) -> None:
        adapter = csv_adapter()
        path = CSV_SAMPLES / "branch_incidents.jsonl"
        first = list(adapter.read_file(path, imported_at=IMPORTED_AT))
        second = list(adapter.read_file(path))
        assert len(first) == 3
        keys = [e.external_key for e in first]
        assert keys == [e.external_key for e in second]
        assert all(k is not None and k.startswith("sha256-") and len(k) == 39 for k in keys)
        assert len(set(keys)) == 3
        assert all(e.event_type is IncidentEventType.RECORD_CREATED for e in first)
        assert first[0].raw_event_name == "jsonl_import"
        assert first[1].fields["created_at"] == datetime(2026, 10, 15, 7, 5, tzinfo=UTC)
        assert first[2].mapped_fields == {}
        assert first[2].group_key == "branch_incidents"

    def test_jsonl_edited_row_gets_new_key(self, tmp_path: Path) -> None:
        path = tmp_path / "rows.jsonl"
        path.write_text(json.dumps({"Konu": "A", "Durum": "Açık"}) + "\n\n", encoding="utf-8")
        (a,) = csv_adapter().read_file(path)
        path.write_text(json.dumps({"Durum": " Açık ", "Konu": "A"}) + "\n", encoding="utf-8")
        (same,) = csv_adapter().read_file(path)
        path.write_text(json.dumps({"Konu": "A", "Durum": "Kapalı"}) + "\n", encoding="utf-8")
        (edited,) = csv_adapter().read_file(path)
        assert a.external_key == same.external_key != edited.external_key

    def test_bad_jsonl_line(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text('{"Konu": "ok"}\n{broken\n', encoding="utf-8")
        with pytest.raises(SourcePayloadError, match=r"bad\.jsonl line 2"):
            list(csv_adapter().read_file(path))

    def test_custom_datetime_format_and_timezone(self, tmp_path: Path) -> None:
        path = tmp_path / "dates.csv"
        path.write_text("Konu;Oluşturma\nx;2026/10/15 08:00\n", encoding="utf-8")
        adapter = csv_adapter(datetime_formats=["%Y/%m/%d %H:%M"], timezone="UTC")
        (ev,) = adapter.read_file(path)
        assert ev.fields["created_at"] == datetime(2026, 10, 15, 8, 0, tzinfo=UTC)

    def test_normalize_envelope_round_trip_with_masking(self) -> None:
        adapter = csv_adapter(id_column="Kayıt No")
        event = next(iter(adapter.read_file(CSV_SAMPLES / "branch_incidents.csv", imported_at=IMPORTED_AT)))
        envelope = json.loads(json.dumps(event_envelope(event)))
        restored = adapter.normalize(envelope)
        assert restored == event
        assert isinstance(restored.fields["created_at"], datetime)
        masked = adapter.normalize(envelope, transform=_mask)
        assert masked.mapped_fields["branch_code"] == "****"
        with pytest.raises(SourcePayloadError):
            adapter.normalize({"not": "an envelope"})
        with pytest.raises(SourcePayloadError):
            csv_adapter().normalize({"incident_event": {**envelope["incident_event"], "source": "other"}})

    def test_fetch_watch_directory(self, tmp_path: Path) -> None:
        old = tmp_path / "a_old.jsonl"
        old.write_text('{"Konu": "eski 1"}\n{"Konu": "eski 2"}\n', encoding="utf-8")
        new = tmp_path / "b_new.csv"
        new.write_text("Konu\nyeni 1\nyeni 2\n", encoding="utf-8")
        (tmp_path / "notes.md").write_text("ignored", encoding="utf-8")
        t_old = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
        t_new = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
        os.utime(old, (t_old.timestamp(), t_old.timestamp()))
        os.utime(new, (t_new.timestamp(), t_new.timestamp()))
        adapter = csv_adapter(watch_directory=str(tmp_path))

        everything = list(adapter.fetch(None, 100))
        assert [e.fields["summary"] for e in everything] == ["eski 1", "eski 2", "yeni 1", "yeni 2"]
        assert [e.event_timestamp for e in everything] == [t_old, t_old, t_new, t_new]

        limited = list(adapter.fetch(None, 1))
        assert [e.fields["summary"] for e in limited] == ["eski 1", "eski 2"]  # files are never split
        checkpoint = max(e.source_updated_at or e.event_timestamp for e in limited if e.event_timestamp)
        rest = list(adapter.fetch(checkpoint, 1))
        assert [e.fields["summary"] for e in rest] == ["yeni 1", "yeni 2"]
        assert list(adapter.fetch(t_new, 10)) == []
        assert list(adapter.fetch(t_old - timedelta(seconds=1), 0)) == []

    def test_fetch_without_watch_directory_yields_nothing(self) -> None:
        assert list(csv_adapter().fetch(None, 10)) == []

    def test_xlsx_round_trip(self, tmp_path: Path) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Kayitlar"
        sheet.append(["Kayıt No", "Konu", "Sube Kodu", "Oluşturma"])
        sheet.append([1001, "Kasa raporu yazdırılamıyor", 1234, datetime(2026, 10, 14, 17, 45)])
        sheet.append([None, None, None, None])
        path = tmp_path / "import.xlsx"
        workbook.save(path)
        (ev,) = csv_adapter(id_column="Kayıt No", sheet="Kayitlar").read_file(path)
        assert ev.external_key == "1001"
        assert ev.raw_event_name == "xlsx_import"
        assert ev.mapped_fields == {"branch_code": "1234"}
        assert ev.fields["created_at"] == datetime(2026, 10, 14, 17, 45, tzinfo=IST)

    def test_xlsx_without_openpyxl_gives_clear_error(self, tmp_path: Path) -> None:
        if importlib.util.find_spec("openpyxl") is not None:
            pytest.skip("openpyxl is installed")
        path = tmp_path / "x.xlsx"
        path.write_bytes(b"")
        with pytest.raises(SourcePayloadError, match="openpyxl"):
            list(csv_adapter().read_file(path))


# -- registry -------------------------------------------------------------------------------------------


def test_build_source_registry() -> None:
    settings = Settings.model_validate(
        {
            "sources": [
                {"id": "jira", "type": "jira_webhook"},
                {
                    "id": "snow",
                    "type": "servicenow_webhook",
                    "auth": {"mode": "token", "secrets_env": SNOW_SECRETS_ENV},
                    "field_mapping": {"branch_code": "u_branch_code"},
                },
                {"id": "csv", "type": "csv_import", "options": {"id_column": "Kayıt No"}},
                {"id": "old-csv", "type": "csv_import", "enabled": False},
            ]
        }
    )
    registry = build_source_registry(settings)
    assert isinstance(registry.get("snow"), ServiceNowWebhookAdapter)
    assert isinstance(registry.get("csv"), CsvImportAdapter)
    assert {a.source_id for a in registry.push_sources()} == {"jira", "snow"}
    assert [a.source_id for a in registry.pull_sources()] == ["csv"]
    assert registry.get_pull("csv").poll_interval_seconds == 300.0
    with pytest.raises(UnknownSourceError):
        registry.get("old-csv")
    with pytest.raises(UnknownSourceError):
        registry.get_pull("snow")
