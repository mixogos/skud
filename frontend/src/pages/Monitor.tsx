import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get, Page } from "../api";
import { useApp } from "../ctx";
import { Empty, ErrorBox, Modal, Spinner } from "../ui";

interface Camera {
  id: number; name: string; group_id: number; state: string; state_message: string;
  fps: number; width: number; height: number; active: boolean;
}

const STATES: Record<string, [string, string]> = {
  DISABLED: ["выключена", ""], CONNECTING: ["подключение", "warn"], ONLINE: ["на связи", "ok"], ERROR: ["нет связи", "bad"],
};

export default function MonitorPage() {
  const { groupById } = useApp();
  const [cameras, setCameras] = useState<Camera[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [bigId, setBigId] = useState<number | null>(null);

  // Список и состояния камер обновляются сами, чтобы плитки оживали при подключении.
  useEffect(() => {
    let alive = true;
    const load = () => get<Page<Camera>>("/cameras/", { external_detector: false }).then(
      (p) => { if (alive) { setCameras(p.results); setError(null); } },
      (e) => { if (alive) setError(e.message); },
    );
    load();
    const timer = setInterval(load, 4000);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  const big = bigId != null ? cameras?.find((c) => c.id === bigId) ?? null : null;

  return (
    <section>
      <div className="toolbar">
        <div className="grow">
          <h2>Онлайн-видео</h2>
          <div className="small muted">Живое изображение со всех камер. Видео идёт, пока камера «на связи». Нажмите на плитку, чтобы открыть крупно.</div>
        </div>
      </div>
      <ErrorBox>{error}</ErrorBox>
      {!cameras && !error && <Spinner />}
      {cameras && (cameras.length === 0
        ? <Empty>Камер нет. Добавьте камеру в разделе <Link to="/videos">«Источники видео»</Link>.</Empty>
        : <div className="monitor-grid">
            {cameras.map((c) => (
              <Tile key={c.id} cam={c} groupName={groupById.get(c.group_id)?.name} onOpen={() => setBigId(c.id)} />
            ))}
          </div>)}
      {big && (
        <Modal wide onClose={() => setBigId(null)} title={<span>{big.name}{groupName(groupById, big) && <span className="muted small"> · {groupName(groupById, big)}</span>}</span>}>
          <div className="monitor-screen big"><Feed cam={big} /></div>
          <div className="small muted" style={{ marginTop: 8 }}>
            {big.state === "ONLINE" ? `${big.width}×${big.height}, ${big.fps.toFixed(0)} кадр/с` : (STATES[big.state]?.[0] ?? big.state)}
          </div>
        </Modal>
      )}
    </section>
  );
}

function groupName(groupById: Map<number, { name: string }>, cam: Camera): string | undefined {
  return groupById.get(cam.group_id)?.name;
}

/** Живой кадр: пока камера «на связи», браузер держит открытым MJPEG-поток в теге img. */
function Feed({ cam }: { cam: Camera }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [cam.state]);
  if (cam.state === "ONLINE" && !failed) {
    // key по состоянию: при возврате «на связи» поток пересоздаётся заново.
    return (
      <img
        key={cam.state} className="monitor-video" src={`/cameras/${cam.id}/mjpeg/`}
        alt={`Видео с камеры «${cam.name}»`} onError={() => setFailed(true)}
      />
    );
  }
  const label = failed ? "нет сигнала" : STATES[cam.state]?.[0] ?? cam.state;
  return (
    <div className="feed-off">
      <div>{label}</div>
      {cam.state_message && <div className="small" style={{ marginTop: 4, maxWidth: 300 }}>{cam.state_message}</div>}
    </div>
  );
}

function Tile({ cam, groupName, onOpen }: { cam: Camera; groupName?: string; onOpen: () => void }) {
  const [label, tone] = STATES[cam.state] ?? [cam.state, ""];
  return (
    <button className="monitor-tile" onClick={onOpen} title="Открыть крупно">
      <div className="monitor-screen"><Feed cam={cam} /></div>
      <div className="monitor-cap spread">
        <span className="ellipsis"><strong>{cam.name}</strong>{groupName && <span className="muted small"> · {groupName}</span>}</span>
        <span className="row" style={{ gap: 6, flex: "none" }}>
          {cam.state === "ONLINE" && <span className="mono small muted">{cam.fps.toFixed(0)} к/с</span>}
          <span className={`badge ${tone}`}>{label}</span>
        </span>
      </div>
    </button>
  );
}
