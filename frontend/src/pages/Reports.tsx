import { useState } from "react";
import { api, get, useLoad } from "../api";
import { useApp } from "../ctx";
import { Empty, ErrorBox, Field, Icon, Spinner, useToast } from "../ui";

interface Row { card_id: number; card_name: string; date: string; first_seen: string; last_seen: string; duration_seconds: number; events: number }

const isoDay = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const hoursMinutes = (seconds: number) => `${Math.floor(seconds / 3600)}:${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}`;
const ruDate = (iso: string) => iso.split("-").reverse().join(".");

export default function ReportsPage() {
  const { lists, groups } = useApp();
  const toast = useToast();
  const today = new Date();
  const [from, setFrom] = useState(isoDay(new Date(today.getFullYear(), today.getMonth(), 1)));
  const [to, setTo] = useState(isoDay(today));
  const [listId, setListId] = useState("");
  const [groupId, setGroupId] = useState("");
  const query = { date_from: from, date_to: to, watch_lists: listId ? [listId] : undefined, camera_groups: groupId ? [groupId] : undefined };
  const report = useLoad<{ count: number; results: Row[] }>(() => get("/reports/worktime/", query), [from, to, listId, groupId]);

  const download = async () => {
    try {
      const response = await api<Response>("GET", "/reports/worktime/", { query: { ...query, format: "csv" }, raw: true });
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `worktime_${from}_${to}.csv`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e: any) { toast(e.message, true); }
  };

  const total = report.data?.results.reduce((sum, r) => sum + r.duration_seconds, 0) ?? 0;

  return (
    <>
      <div className="toolbar">
        <Field label="С"><input className="input" type="date" value={from} max={to} onChange={(e) => e.target.value && setFrom(e.target.value)} /></Field>
        <Field label="По"><input className="input" type="date" value={to} min={from} onChange={(e) => e.target.value && setTo(e.target.value)} /></Field>
        <Field label="Список наблюдения">
          <select className="input" value={listId} onChange={(e) => setListId(e.target.value)}>
            <option value="">Любой</option>
            {lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </Field>
        <Field label="Группа камер">
          <select className="input" value={groupId} onChange={(e) => setGroupId(e.target.value)}>
            <option value="">Любая</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Field>
        <div className="grow" />
        <button className="btn" onClick={download} disabled={!report.data?.count}><Icon name="upload" size={15} />Скачать CSV</button>
      </div>
      <div className="muted small" style={{ marginBottom: 12 }}>
        Рабочее время: приход — первое появление человека за день, уход — последнее. Учитываются события с камер и внешних детекторов; события из видеофайлов не учитываются.
      </div>
      <ErrorBox>{report.error}</ErrorBox>
      {!report.data && report.loading && <Spinner />}
      {report.data && (report.data.results.length === 0 ? <Empty>За выбранный период событий с совпадением нет.</Empty> : (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Дата</th><th>Сотрудник</th><th>Приход</th><th>Уход</th><th>Время, ч:мм</th><th>Событий</th></tr></thead>
          <tbody>
            {report.data.results.map((r) => (
              <tr key={`${r.card_id}-${r.date}`}>
                <td className="mono">{ruDate(r.date)}</td>
                <td><strong>{r.card_name}</strong></td>
                <td className="mono">{r.first_seen}</td>
                <td className="mono">{r.last_seen}</td>
                <td className="mono">{hoursMinutes(r.duration_seconds)}</td>
                <td className="mono muted">{r.events}</td>
              </tr>
            ))}
            <tr><td colSpan={4} className="muted">Итого за период</td><td className="mono"><strong>{hoursMinutes(total)}</strong></td><td /></tr>
          </tbody>
        </table></div>
      ))}
    </>
  );
}
