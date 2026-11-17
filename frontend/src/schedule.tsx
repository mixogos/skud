// Редактор расписания прохода: один интервал на день недели.
export type Schedule = Record<string, [string, string][]>;

const DAYS: [string, string][] = [
  ["monday", "Пн"], ["tuesday", "Вт"], ["wednesday", "Ср"], ["thursday", "Чт"], ["friday", "Пт"], ["saturday", "Сб"], ["sunday", "Вс"],
];

export function describeSchedule(schedule: Schedule | null | undefined): string {
  if (!schedule || !Object.keys(schedule).length) return "круглосуточно";
  return DAYS.filter(([key]) => schedule[key]?.length).map(([key, label]) => `${label} ${schedule[key].map((i) => i.join("–")).join(", ")}`).join("; ");
}

export function ScheduleEditor({ value, onChange }: { value: Schedule; onChange: (v: Schedule) => void }) {
  const restricted = Object.keys(value).length > 0;
  const setDay = (day: string, interval: [string, string] | null) => {
    const next = { ...value };
    if (interval) next[day] = [interval]; else delete next[day];
    onChange(next);
  };
  return (
    <div className="col" style={{ gap: 8 }}>
      <label className="check">
        <input type="checkbox" checked={restricted}
          onChange={(e) => onChange(e.target.checked ? Object.fromEntries(DAYS.slice(0, 5).map(([d]) => [d, [["08:00", "20:00"]]])) as Schedule : {})} />
        <span>Ограничить проход по времени</span>
      </label>
      {restricted && (
        <div className="col" style={{ gap: 4 }}>
          {DAYS.map(([day, label]) => {
            const interval = value[day]?.[0];
            return (
              <div key={day} className="row">
                <label className="check" style={{ width: 54 }}>
                  <input type="checkbox" checked={!!interval} onChange={(e) => setDay(day, e.target.checked ? ["08:00", "20:00"] : null)} />
                  <span>{label}</span>
                </label>
                {interval ? <>
                  <input className="input" style={{ width: 110 }} type="time" value={interval[0]} aria-label={`${label}: начало`}
                    onChange={(e) => e.target.value && setDay(day, [e.target.value, interval[1]])} />
                  <span className="muted">—</span>
                  <input className="input" style={{ width: 110 }} type="time" value={interval[1] === "24:00" ? "23:59" : interval[1]} aria-label={`${label}: конец`}
                    onChange={(e) => e.target.value && setDay(day, [interval[0], e.target.value])} />
                </> : <span className="muted small">прохода нет</span>}
              </div>
            );
          })}
          {Object.values(value).some((i) => i[0][0] >= i[0][1]) && <div className="small" style={{ color: "var(--danger)" }}>Начало интервала должно быть раньше конца.</div>}
        </div>
      )}
    </div>
  );
}
