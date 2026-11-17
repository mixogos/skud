import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fmtSmart, get, Page, patch, pct, useLoad } from "../api";
import { ListChips, useApp, useFeed } from "../ctx";
import { Empty, ErrorBox, Face, Field, Icon, Pager, Seg, Spinner, useAction } from "../ui";

interface Episode {
  id: number; created_date: string; last_event_date: string; open: boolean; events_count: number; best_event_id: number | null;
  matched: boolean; matched_card_id: number | null; matched_card_name: string | null; confidence: number | null;
  matched_lists: number[]; camera_groups: number[]; acknowledged: boolean; video_archive_id: number | null;
}

const LIMIT = 60;

export default function EpisodesPage() {
  const { lists, can, groupById, refreshPending } = useApp();
  const navigate = useNavigate();
  const run = useAction();
  const [state, setState] = useState("");
  const [matched, setMatched] = useState("");
  const [ack, setAck] = useState("");
  const [listId, setListId] = useState("");
  const [offset, setOffset] = useState(0);
  const change = (setter: (v: string) => void) => (v: string) => { setter(v); setOffset(0); };

  const list = useLoad<Page<Episode>>(
    () => get("/episodes/humans/", { open: state, matched, acknowledged: ack, watch_lists: listId ? [listId] : undefined, limit: LIMIT, offset }),
    [state, matched, ack, listId, offset],
  );

  // Обновления эпизодов приходят часто — перезагружаем список не чаще раза в секунду.
  const timer = useRef<number>();
  useFeed((m) => {
    if (m.type !== "human_episode_updated" && m.type !== "events_acknowledged_all") return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(list.reload, 1000);
  });

  const acknowledge = async (episode: Episode) => {
    if (await run(() => patch(`/episodes/humans/${episode.id}/`, { acknowledged: true }), "Эпизод принят")) { list.reload(); refreshPending(); }
  };

  return (
    <>
      <div className="toolbar">
        <Field label="Состояние">
          <Seg value={state} onChange={change(setState)} options={[["", "Все"], ["true", "Идут"], ["false", "Закрыты"]]} />
        </Field>
        <Field label="Совпадение">
          <Seg value={matched} onChange={change(setMatched)} options={[["", "Все"], ["true", "Есть"], ["false", "Нет"]]} />
        </Field>
        <Field label="Принятие">
          <Seg value={ack} onChange={change(setAck)} options={[["", "Все"], ["false", "Ждут"], ["true", "Приняты"]]} />
        </Field>
        <Field label="Список наблюдения">
          <select className="input" value={listId} onChange={(e) => change(setListId)(e.target.value)}>
            <option value="">Любой</option>
            {lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </Field>
      </div>

      <ErrorBox>{list.error}</ErrorBox>
      {!list.data && list.loading && <Spinner />}
      {list.data && (list.data.results.length === 0
        ? <Empty>Эпизодов нет. Эпизод объединяет события одного человека, идущие подряд.</Empty>
        : <>
            <div className="small muted" style={{ marginBottom: 8 }}>Всего: <span className="mono">{list.data.count}</span></div>
            <div className="events">
              {list.data.results.map((ep) => (
                <div key={ep.id} className={`event${ep.matched ? " matched" : ""}${ep.acknowledged ? "" : " pending"}`} role="button" tabIndex={0}
                  onClick={() => navigate(`/events?episode=${ep.id}`)} onKeyDown={(e) => e.key === "Enter" && navigate(`/events?episode=${ep.id}`)}>
                  <Face src={ep.best_event_id ? `/events/faces/${ep.best_event_id}/thumbnail/` : null} size={72} alt="Лучший кадр эпизода" />
                  <div className="grow col" style={{ gap: 4 }}>
                    <div className="spread">
                      <strong className="ellipsis">{ep.matched ? ep.matched_card_name ?? "Карточка удалена" : "Нет совпадения"}</strong>
                      {ep.open ? <span className="badge ok">идёт</span> : ep.confidence != null && <span className="conf">{pct(ep.confidence)}</span>}
                    </div>
                    <ListChips ids={ep.matched_lists} />
                    <div className="small muted mono">{fmtSmart(ep.created_date)} · событий: {ep.events_count}</div>
                    <div className="spread small muted">
                      <span className="ellipsis">{ep.camera_groups.map((g) => groupById.get(g)?.name ?? g).join(", ")}</span>
                      {!ep.acknowledged && (can("humanepisode:change", "faceevent:change")
                        ? <button className="btn sm" onClick={(e) => { e.stopPropagation(); acknowledge(ep); }}><Icon name="check" size={13} />Принять</button>
                        : <span className="badge warn">ждёт принятия</span>)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
            <Pager count={list.data.count} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>)}
    </>
  );
}
