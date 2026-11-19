import { useRef, useState } from "react";
import { get, fmtSmart, Page, pct, useLoad } from "../api";
import { useApp, useFeed } from "../ctx";
import { Empty, ErrorBox, Face, Field, Seg, Spinner, Pager } from "../ui";

interface PersonEvent {
  id: number; created_date: string; camera_group_id: number | null; camera_id: number | null;
  track_duration: number; detection_score: number;
  gender: "male" | "female" | null; gender_confidence: number | null;
  attrs: Record<string, number>; upper_color: string | null; lower_color: string | null;
}

const ATTR_LABELS: Record<string, string> = {
  hat: "головной убор", bag: "сумка", coat: "куртка",
  longsleeves: "длинные рукава", longpants: "длинные штаны", longhair: "длинные волосы",
};
const COLOR_LABELS: Record<string, string> = {
  black: "чёрный", white: "белый", gray: "серый", brown: "коричневый", red: "красный", orange: "оранжевый",
  yellow: "жёлтый", green: "зелёный", cyan: "голубой", blue: "синий", purple: "фиолетовый", pink: "розовый",
};
const COLOR_HEX: Record<string, string> = {
  black: "#222", white: "#eee", gray: "#8a929e", brown: "#8a5a2e", red: "#d64141", orange: "#e8822e",
  yellow: "#e8c62e", green: "#3a9d3a", cyan: "#2eb8c6", blue: "#3461d6", purple: "#8a4fd6", pink: "#d66fae",
};

function Swatch({ color, label }: { color: string | null; label: string }) {
  if (!color || !COLOR_HEX[color]) return null;
  return (
    <span className="row" style={{ gap: 5 }} title={`${label}: ${COLOR_LABELS[color] ?? color}`}>
      <i style={{ width: 13, height: 13, borderRadius: 3, background: COLOR_HEX[color], border: "1px solid var(--border)", display: "inline-block" }} />
      <span className="small muted">{label} {COLOR_LABELS[color] ?? color}</span>
    </span>
  );
}

const LIMIT = 60;

export default function PeoplePage() {
  const { groupById } = useApp();
  const [gender, setGender] = useState("");
  const [has, setHas] = useState("");
  const [offset, setOffset] = useState(0);

  const list = useLoad<Page<PersonEvent>>(
    () => get("/events/people/", { gender, has, limit: LIMIT, offset }),
    [gender, has, offset],
  );
  const dataRef = useRef(list.data);
  dataRef.current = list.data;
  // Новые события людей добавляются в начало списка, если подходят под фильтры.
  useFeed((m) => {
    if (m.type !== "person_event_created" || !dataRef.current || offset !== 0) return;
    const e: PersonEvent = m.data;
    if (gender && e.gender !== gender) return;
    if (has && !((e.attrs?.[has] ?? 0) >= 0.5)) return;
    list.setData({ count: dataRef.current.count + 1, results: [e, ...dataRef.current.results].slice(0, 200) });
  });

  return (
    <>
      <div className="toolbar">
        <div className="grow">
          <h2>Люди</h2>
          <div className="small muted">События по силуэтам: пол, одежда, аксессуары и цвет одежды. Ловит и тех, кто стоит спиной. Включается в группе камер.</div>
        </div>
        <Field label="Пол">
          <Seg value={gender} onChange={(v) => { setGender(v); setOffset(0); }} options={[["", "Все"], ["male", "Муж."], ["female", "Жен."]]} />
        </Field>
        <Field label="Признак">
          <select className="input" value={has} onChange={(e) => { setHas(e.target.value); setOffset(0); }}>
            <option value="">Любой</option>
            {Object.entries(ATTR_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </Field>
      </div>

      <ErrorBox>{list.error}</ErrorBox>
      {!list.data && list.loading && <Spinner />}
      {list.data && (list.data.results.length === 0
        ? <Empty>Событий по людям нет. Включите «Определять людей и одежду» в группе камер (раздел «Источники видео»).</Empty>
        : <>
            <div className="small muted" style={{ marginBottom: 8 }}>Всего: <span className="mono">{list.data.count}</span></div>
            <div className="events">
              {list.data.results.map((e) => <PersonTile key={e.id} event={e} groupName={e.camera_group_id ? groupById.get(e.camera_group_id)?.name : undefined} />)}
            </div>
            <Pager count={list.data.count} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>)}
    </>
  );
}

function PersonTile({ event, groupName }: { event: PersonEvent; groupName?: string }) {
  const present = Object.entries(event.attrs || {}).filter(([, v]) => v >= 0.5).map(([k]) => ATTR_LABELS[k]).filter(Boolean);
  return (
    <div className="event">
      <Face src={`/events/people/${event.id}/image/`} size={96} alt="Человек с камеры" />
      <div className="grow col" style={{ gap: 5 }}>
        <div className="spread">
          <strong>{event.gender === "male" ? "Мужчина" : event.gender === "female" ? "Женщина" : "Человек"}</strong>
          {event.gender_confidence != null && <span className="conf" title="Уверенность в поле">{pct(event.gender_confidence)}</span>}
        </div>
        <div className="row wrap" style={{ gap: 10 }}>
          <Swatch color={event.upper_color} label="верх" />
          <Swatch color={event.lower_color} label="низ" />
        </div>
        {present.length > 0 && <div className="row wrap" style={{ gap: 4 }}>{present.map((p) => <span key={p} className="badge">{p}</span>)}</div>}
        <div className="spread small muted">
          <span className="mono">{fmtSmart(event.created_date)}</span>
          <span className="ellipsis">{groupName ?? ""}</span>
        </div>
      </div>
    </div>
  );
}
