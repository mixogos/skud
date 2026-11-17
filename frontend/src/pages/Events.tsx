import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { fmtDate, fmtSmart, get, Page, patch, pct, post, postForm, useLoad } from "../api";
import { beep, ListChips, useApp, useFeed } from "../ctx";
import { Empty, ErrorBox, Face, Field, Icon, Modal, Pager, Seg, Spinner, useAction, useBlob } from "../ui";

export interface FaceEvent {
  id: number; created_date: string; camera_group_id: number | null; video_archive_id: number | null; video_offset: number | null;
  track_duration: number; track_frames: number; bbox: number[]; frame_width: number; frame_height: number;
  detection_score: number; quality: number; matched: boolean; matched_card_id: number | null; matched_card_name: string | null;
  matched_face_id: number | null; confidence: number | null; matched_lists: number[]; acknowledged: boolean;
  acknowledged_date: string | null; acknowledged_reaction: string; episode_id: number | null; looks_like_confidence?: number | null;
  access: "granted" | "denied" | null; access_reason: string; camera_id: number | null; liveness: number | null;
  features: { age?: number; age_group?: string; gender?: { name: "male" | "female"; confidence: number }; medmask?: { name: "none" | "correct"; confidence: number } };
}

/** Краткая строка атрибутов лица: «жен., 28 лет, в маске». */
export function featuresText(event: FaceEvent): string {
  const f = event.features ?? {};
  const parts: string[] = [];
  if (f.gender) parts.push(f.gender.name === "male" ? "муж." : "жен.");
  if (f.age_group) parts.push(`${f.age_group} лет`);
  else if (f.age != null) parts.push(`${f.age} ${f.age % 10 === 1 && f.age % 100 !== 11 ? "год" : f.age % 10 >= 2 && f.age % 10 <= 4 && (f.age % 100 < 12 || f.age % 100 > 14) ? "года" : "лет"}`);
  if (f.medmask?.name === "correct") parts.push("в маске");
  return parts.join(", ");
}

const LIMIT = 60;
const FILTER_KEYS = ["matched", "list", "group", "ack", "access", "name", "matched_card", "episode", "looks_like", "video", "gender", "mask", "age_from", "age_to", "id"] as const;

export function AccessBadge({ event }: { event: FaceEvent }) {
  if (!event.access) return null;
  return <span className={`badge ${event.access === "granted" ? "ok" : "bad"}`} title={event.access_reason}>{event.access === "granted" ? "проход разрешён" : "отказ"}</span>;
}

export function EventTile({ event, onOpen, fresh }: { event: FaceEvent; onOpen: () => void; fresh?: boolean }) {
  const { groupById } = useApp();
  const cls = `event${event.matched ? " matched" : ""}${event.acknowledged ? "" : " pending"}${fresh ? " fresh" : ""}`;
  return (
    <button className={cls} onClick={onOpen}>
      <div className="pair">
        <Face src={`/events/faces/${event.id}/thumbnail/`} size={72} alt="Лицо с камеры" />
        {event.matched_face_id && <Face src={`/objects/faces/${event.matched_face_id}/thumbnail/`} size={72} alt="Фото из карточки" />}
      </div>
      <div className="grow col" style={{ gap: 4 }}>
        <div className="spread">
          <strong className="ellipsis">{event.matched ? event.matched_card_name ?? "Карточка удалена" : "Нет совпадения"}</strong>
          {event.looks_like_confidence != null
            ? <span className="conf" title="Схожесть с искомым лицом">{pct(event.looks_like_confidence)}</span>
            : event.confidence != null && <span className="conf" title="Схожесть с карточкой">{pct(event.confidence)}</span>}
        </div>
        <ListChips ids={event.matched_lists} />
        <div className="spread small muted"><span className="mono">{fmtSmart(event.created_date)}</span><span>{featuresText(event)}</span></div>
        <div className="spread small muted">
          <span className="ellipsis">{event.camera_group_id ? groupById.get(event.camera_group_id)?.name ?? "" : ""}</span>
          <span className="row" style={{ gap: 4 }}><AccessBadge event={event} />{!event.acknowledged && <span className="badge warn">ждёт принятия</span>}</span>
        </div>
      </div>
    </button>
  );
}

export default function EventsPage() {
  const { lists, groups, can, refreshPending, listById } = useApp();
  const [params, setParams] = useSearchParams();
  const [offset, setOffset] = useState(0);
  const [paused, setPaused] = useState(false);
  const [opened, setOpened] = useState<FaceEvent | null>(null);
  const [freshIds, setFreshIds] = useState<Set<number>>(new Set());
  const [missed, setMissed] = useState(0);
  const run = useAction();

  const f = Object.fromEntries(FILTER_KEYS.map((k) => [k, params.get(k) ?? ""])) as Record<(typeof FILTER_KEYS)[number], string>;
  const filterKey = FILTER_KEYS.map((k) => f[k]).join("|");
  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    setParams(next, { replace: true });
    setOffset(0);
  };
  const [nameDraft, setNameDraft] = useState(f.name);
  useEffect(() => setNameDraft(f.name), [f.name]);
  useEffect(() => {
    if (nameDraft === f.name) return;
    const timer = setTimeout(() => setFilter("name", nameDraft), 350);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nameDraft]);

  const list = useLoad<Page<FaceEvent>>(() => {
    setMissed(0);
    return get("/events/faces/", {
      matched: f.matched, watch_lists: f.list ? [f.list] : undefined, camera_groups: f.group ? [f.group] : undefined,
      acknowledged: f.ack, card_name_contains: f.name, matched_card: f.matched_card, episode: f.episode,
      looks_like: f.looks_like, video_archive: f.video, access: f.access, gender: f.gender, medmask: f.mask,
      age_gte: f.age_from, age_lte: f.age_to, id_in: f.id ? [f.id] : undefined, limit: LIMIT, offset,
    });
  }, [filterKey, offset]);

  // Новые события из ленты добавляются в начало списка, если подходят под простые фильтры.
  const live = offset === 0 && !f.looks_like && !f.name && !f.episode && !f.id;
  const accepts = (e: FaceEvent) =>
    (!f.matched || String(e.matched) === f.matched) && (!f.ack || String(e.acknowledged) === f.ack) && (!f.access || e.access === f.access) &&
    (!f.list || e.matched_lists.includes(Number(f.list))) && (!f.group || e.camera_group_id === Number(f.group)) &&
    (!f.matched_card || e.matched_card_id === Number(f.matched_card)) && (!f.video || e.video_archive_id === Number(f.video)) &&
    (!f.gender || e.features?.gender?.name === f.gender) && (!f.mask || e.features?.medmask?.name === f.mask) &&
    (!f.age_from || (e.features?.age ?? -1) >= Number(f.age_from)) && (!f.age_to || (e.features?.age ?? 999) <= Number(f.age_to));
  const dataRef = useRef(list.data);
  dataRef.current = list.data;
  useFeed((message) => {
    const data = dataRef.current;
    if (message.type === "events_acknowledged_all") { list.reload(); return; }
    if (!data) return;
    const event: FaceEvent = message.data;
    if (message.type === "face_event_created") {
      if (event.matched_lists.some((id) => listById.get(id)?.notify)) beep();
      if (!accepts(event)) return;
      if (!live || paused) { setMissed((n) => n + 1); return; }
      list.setData({ count: data.count + 1, results: [event, ...data.results].slice(0, 200) });
      setFreshIds((ids) => new Set(ids).add(event.id));
    } else if (message.type === "face_event_updated") {
      list.setData({ ...data, results: data.results.map((e) => (e.id === event.id ? { ...e, ...event, looks_like_confidence: e.looks_like_confidence } : e)) });
      setOpened((o) => (o && o.id === event.id ? { ...o, ...event } : o));
    }
  });

  const acknowledgeAll = async () => {
    if (!confirm("Принять все непринятые события?")) return;
    if (await run(() => post("/events/faces/acknowledge/"), "Все события приняты")) { list.reload(); refreshPending(); }
  };

  const extra = [
    f.matched_card && ["matched_card", `Карточка № ${f.matched_card}`],
    f.episode && ["episode", `Эпизод № ${f.episode}`],
    f.video && ["video", `Видео № ${f.video}`],
    f.id && ["id", `Событие № ${f.id}`],
    f.looks_like && ["looks_like", "Поиск по лицу"],
  ].filter(Boolean) as [string, string][];

  return (
    <>
      <div className="toolbar">
        <Field label="Совпадение">
          <Seg value={f.matched} onChange={(v) => setFilter("matched", v)} options={[["", "Все"], ["true", "Есть"], ["false", "Нет"]]} />
        </Field>
        <Field label="Список наблюдения">
          <select className="input" value={f.list} onChange={(e) => setFilter("list", e.target.value)}>
            <option value="">Любой</option>
            {lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </Field>
        <Field label="Группа камер">
          <select className="input" value={f.group} onChange={(e) => setFilter("group", e.target.value)}>
            <option value="">Любая</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Field>
        <Field label="Принятие">
          <Seg value={f.ack} onChange={(v) => setFilter("ack", v)} options={[["", "Все"], ["false", "Ждут"], ["true", "Приняты"]]} />
        </Field>
        <Field label="Проход">
          <Seg value={f.access} onChange={(v) => setFilter("access", v)} options={[["", "Все"], ["granted", "Разрешён"], ["denied", "Отказ"]]} />
        </Field>
        <Field label="Имя в карточке">
          <input className="input" value={nameDraft} onChange={(e) => setNameDraft(e.target.value)} placeholder="Поиск по имени" />
        </Field>
        <Field label="Пол">
          <Seg value={f.gender} onChange={(v) => setFilter("gender", v)} options={[["", "Любой"], ["male", "Муж."], ["female", "Жен."]]} />
        </Field>
        <Field label="Возраст, лет">
          <div className="row" style={{ gap: 4 }}>
            <input className="input" type="number" min={0} max={100} placeholder="от" aria-label="Возраст от" value={f.age_from} onChange={(e) => setFilter("age_from", e.target.value)} />
            <input className="input" type="number" min={0} max={100} placeholder="до" aria-label="Возраст до" value={f.age_to} onChange={(e) => setFilter("age_to", e.target.value)} />
          </div>
        </Field>
        <Field label="Маска">
          <Seg value={f.mask} onChange={(v) => setFilter("mask", v)} options={[["", "Все"], ["correct", "Есть"], ["none", "Нет"]]} />
        </Field>
        <div className="grow" />
        <button className="btn" onClick={() => setPaused((p) => !p)} title="Остановить вывод новых событий">
          <Icon name={paused ? "play" : "pause"} size={15} />{paused ? "Продолжить" : "Пауза"}
        </button>
        {can("faceevent:change") && <button className="btn" onClick={acknowledgeAll}><Icon name="check" size={15} />Принять все</button>}
      </div>

      {extra.length > 0 && (
        <div className="row wrap" style={{ marginBottom: 12 }}>
          {extra.map(([key, label]) => (
            <span key={key} className="badge">{label}
              <button className="btn ghost sm" style={{ height: 18, padding: 0 }} onClick={() => setFilter(key, "")} aria-label="Сбросить"><Icon name="close" size={12} /></button>
            </span>
          ))}
        </div>
      )}
      {missed > 0 && (
        <button className="btn" style={{ marginBottom: 12 }} onClick={() => { setPaused(false); setOffset(0); list.reload(); }}>
          Новых событий: {missed} — показать
        </button>
      )}

      <ErrorBox>{list.error}</ErrorBox>
      {!list.data && list.loading && <Spinner />}
      {list.data && (list.data.results.length === 0
        ? <Empty>Событий нет. Они появятся после обработки видео на вкладке «Видео».</Empty>
        : <>
            <div className="small muted" style={{ marginBottom: 8 }}>Всего: <span className="mono">{list.data.count}</span></div>
            <div className="events">
              {list.data.results.map((e) => <EventTile key={e.id} event={e} fresh={freshIds.has(e.id)} onOpen={() => setOpened(e)} />)}
            </div>
            <Pager count={list.data.count} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>)}

      {opened && (
        <EventModal
          event={opened}
          onClose={() => setOpened(null)}
          onChanged={(e) => { setOpened(e); list.reload(); refreshPending(); }}
          onSearch={(id) => { setOpened(null); setParams({ looks_like: `faceevent:${id}` }); setOffset(0); }}
        />
      )}
    </>
  );
}

export function EventModal({ event, onClose, onChanged, onSearch }: { event: FaceEvent; onClose: () => void; onChanged: (e: FaceEvent) => void; onSearch?: (id: number) => void }) {
  const { can, groupById } = useApp();
  const run = useAction();
  const frame = useBlob(`/events/faces/${event.id}/fullframe/`);
  const [reaction, setReaction] = useState("");
  const [creating, setCreating] = useState(false);
  const [left, top, right, bottom] = event.bbox;
  const box = useMemo(() => ({
    left: `${(left / event.frame_width) * 100}%`, top: `${(top / event.frame_height) * 100}%`,
    width: `${((right - left) / event.frame_width) * 100}%`, height: `${((bottom - top) / event.frame_height) * 100}%`,
  }), [left, top, right, bottom, event.frame_width, event.frame_height]);

  const acknowledge = async (value: boolean) => {
    let updated: FaceEvent | undefined;
    if (await run(async () => { updated = await patch(`/events/faces/${event.id}/`, { acknowledged: value, acknowledged_reaction: reaction }); })) onChanged(updated!);
  };

  return (
    <Modal wide onClose={onClose} title={event.matched ? event.matched_card_name ?? "Карточка удалена" : "Нет совпадения с картотекой"}>
      <div className="frame">
        {frame ? <img src={frame} alt="Кадр события" /> : <div style={{ aspectRatio: `${event.frame_width}/${event.frame_height}`, display: "grid", placeItems: "center" }}><Spinner /></div>}
        {frame && <div className="box" style={box} />}
      </div>
      <div className="row wrap" style={{ alignItems: "flex-start", gap: 18 }}>
        <div className="pair">
          <Face src={`/events/faces/${event.id}/thumbnail/`} size={112} alt="Лицо с камеры" />
          {event.matched_face_id && <Face src={`/objects/faces/${event.matched_face_id}/thumbnail/`} size={112} alt="Фото из карточки" />}
        </div>
        <dl className="dl grow">
          <dt>Время</dt><dd className="mono">{fmtDate(event.created_date)}</dd>
          {event.matched && <><dt>Схожесть</dt><dd className="conf">{pct(event.confidence)}</dd></>}
          {event.matched_lists.length > 0 && <><dt>Списки</dt><dd><ListChips ids={event.matched_lists} /></dd></>}
          <dt>Группа камер</dt><dd>{event.camera_group_id ? groupById.get(event.camera_group_id)?.name ?? `№ ${event.camera_group_id}` : "—"}</dd>
          {event.video_archive_id && <><dt>Видеофайл</dt><dd>№ {event.video_archive_id}, <span className="mono">{event.video_offset?.toFixed(1)} с</span> от начала</dd></>}
          {event.access && <><dt>Проход</dt><dd><AccessBadge event={event} /> <span className="small muted">{event.access_reason}</span></dd></>}
          {event.features?.gender && <><dt>Пол</dt><dd>{event.features.gender.name === "male" ? "мужской" : "женский"} <span className="small muted mono">{pct(event.features.gender.confidence)}</span></dd></>}
          {event.features?.age_group
            ? <><dt title="Возрастная группа, оценка по одному кадру">Возраст</dt><dd><span className="mono">{event.features.age_group}</span> лет</dd></>
            : event.features?.age != null && <><dt title="Оценка по одному кадру, ошибка в несколько лет — обычное дело">Возраст</dt><dd>около <span className="mono">{event.features.age}</span></dd></>}
          {event.features?.medmask && <><dt>Маска</dt><dd>{event.features.medmask.name === "correct" ? "есть" : "нет"} <span className="small muted mono">{pct(event.features.medmask.confidence)}</span></dd></>}
          <dt>Качество кадра</dt><dd className="mono">{pct(event.quality)}</dd>
          {event.liveness != null && <><dt title="Вероятность того, что перед камерой живой человек, а не фото или экран">Живость</dt><dd className="mono">{pct(event.liveness)}</dd></>}
          <dt>Проход</dt><dd className="mono">{event.track_duration.toFixed(1)} с, кадров: {event.track_frames}</dd>
          <dt>Событие</dt><dd className="mono">№ {event.id}{event.episode_id ? <>, <Link to={`/events?episode=${event.episode_id}`} onClick={onClose}>эпизод № {event.episode_id}</Link></> : null}</dd>
          <dt>Принятие</dt>
          <dd>{event.acknowledged
            ? <span className="badge ok">принято{event.acknowledged_date ? ` ${fmtDate(event.acknowledged_date)}` : ""}</span>
            : <span className="badge warn">ждёт принятия</span>}
            {event.acknowledged_reaction && <div className="small muted">{event.acknowledged_reaction}</div>}
          </dd>
        </dl>
      </div>
      {!event.acknowledged && can("faceevent:change") && (
        <div className="row">
          <input className="input" placeholder="Комментарий к принятию (необязательно)" maxLength={64} value={reaction} onChange={(e) => setReaction(e.target.value)} />
          <button className="btn primary" onClick={() => acknowledge(true)}><Icon name="check" size={15} />Принять</button>
        </div>
      )}
      <div className="row wrap">
        {onSearch && <button className="btn" onClick={() => onSearch(event.id)}><Icon name="search" size={15} />Где ещё появлялся</button>}
        {event.matched_card_id && <Link className="btn" to={`/cards?open=${event.matched_card_id}`}>Открыть карточку</Link>}
        {can("humancard:add", "faceobject:add") && !event.matched && <button className="btn" onClick={() => setCreating(true)}><Icon name="plus" size={15} />Создать карточку</button>}
        {event.acknowledged && event.acknowledged_date && can("faceevent:change") && <button className="btn ghost" onClick={() => acknowledge(false)}>Снять принятие</button>}
      </div>
      {creating && <CardFromEvent eventId={event.id} onClose={() => setCreating(false)} />}
    </Modal>
  );
}

function CardFromEvent({ eventId, onClose }: { eventId: number; onClose: () => void }) {
  const { lists } = useApp();
  const run = useAction();
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState<number[]>(lists[0] ? [lists[0].id] : []);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    const ok = await run(async () => {
      const card = await post("/cards/humans/", { name, watch_lists: chosen });
      const form = new FormData();
      form.set("card", String(card.id));
      form.set("create_from", `faceevent:${eventId}`);
      await postForm("/objects/faces/", form);
    }, "Карточка создана — следующие события этого человека получат совпадение");
    setBusy(false);
    if (ok) onClose();
  };
  return (
    <Modal title="Новая карточка из события" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Отмена</button>
      <button className="btn primary" disabled={busy || !name.trim() || !chosen.length} onClick={save}>Создать</button>
    </>}>
      <Field label="Имя"><input className="input" value={name} onChange={(e) => setName(e.target.value)} autoFocus maxLength={256} /></Field>
      <Field label="Списки наблюдения">
        <div className="col" style={{ gap: 6 }}>
          {lists.map((l) => (
            <label key={l.id} className="check">
              <input type="checkbox" checked={chosen.includes(l.id)} onChange={(e) => setChosen(e.target.checked ? [...chosen, l.id] : chosen.filter((x) => x !== l.id))} />
              <span className="chip"><i style={{ background: l.color }} />{l.name}</span>
            </label>
          ))}
        </div>
      </Field>
    </Modal>
  );
}
