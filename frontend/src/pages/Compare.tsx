import { ChangeEvent, useEffect, useState } from "react";
import { get, pct, postForm } from "../api";
import { ErrorBox, Field, Spinner } from "../ui";

interface Side { preview: string; faces: { id: string; bbox: { left: number; top: number; right: number; bottom: number } }[]; error: string; busy: boolean; width: number; height: number }
const EMPTY: Side = { preview: "", faces: [], error: "", busy: false, width: 1, height: 1 };

function Slot({ label, side, onFile }: { label: string; side: Side; onFile: (file: File) => void }) {
  const face = side.faces[0];
  return (
    <div className="panel col grow" style={{ minWidth: 260 }}>
      <Field label={label}>
        <input className="input" style={{ padding: "5px 8px" }} type="file" accept="image/jpeg,image/png,image/webp"
          onChange={(e: ChangeEvent<HTMLInputElement>) => e.target.files?.[0] && onFile(e.target.files[0])} />
      </Field>
      {side.busy && <Spinner />}
      <ErrorBox>{side.error}</ErrorBox>
      {side.preview && (
        <div className="frame">
          <img src={side.preview} alt={label} />
          {face && <div className="box" style={{
            left: `${(face.bbox.left / side.width) * 100}%`, top: `${(face.bbox.top / side.height) * 100}%`,
            width: `${((face.bbox.right - face.bbox.left) / side.width) * 100}%`, height: `${((face.bbox.bottom - face.bbox.top) / side.height) * 100}%`,
          }} />}
        </div>
      )}
      {side.faces.length > 1 && <div className="small muted">На фото лиц: {side.faces.length}. Сравнивается самое крупное.</div>}
    </div>
  );
}

export default function ComparePage() {
  const [a, setA] = useState<Side>(EMPTY);
  const [b, setB] = useState<Side>(EMPTY);
  const [result, setResult] = useState<{ confidence: number; threshold: number; match: boolean } | null>(null);
  const [error, setError] = useState("");

  const load = (set: (s: Side) => void) => async (file: File) => {
    const preview = URL.createObjectURL(file);
    const size = await new Promise<{ width: number; height: number }>((resolve) => {
      const img = new Image();
      img.onload = () => resolve({ width: img.naturalWidth, height: img.naturalHeight });
      img.onerror = () => resolve({ width: 1, height: 1 });
      img.src = preview;
    });
    set({ ...EMPTY, preview, busy: true, ...size });
    try {
      const form = new FormData();
      form.set("photo", file);
      const faces = (await postForm("/detect/", form)).objects.face;
      set({ ...EMPTY, preview, faces, ...size, error: faces.length ? "" : "На фото не найдено лицо" });
    } catch (e: any) {
      set({ ...EMPTY, preview, ...size, error: e.message });
    }
  };

  const idA = a.faces[0]?.id, idB = b.faces[0]?.id;
  useEffect(() => {
    setResult(null);
    setError("");
    if (!idA || !idB) return;
    get("/verify/", { object1: idA, object2: idB }).then(setResult, (e) => setError(e.message));
  }, [idA, idB]);

  return (
    <div className="col" style={{ maxWidth: 980 }}>
      <div className="muted">Загрузите два фото, чтобы проверить, один ли на них человек.</div>
      <div className="row wrap" style={{ alignItems: "flex-start", gap: 14 }}>
        <Slot label="Первое фото" side={a} onFile={load(setA)} />
        <Slot label="Второе фото" side={b} onFile={load(setB)} />
      </div>
      <ErrorBox>{error}</ErrorBox>
      {result && (
        <div className="panel row" style={{ gap: 18 }}>
          <div className="mono" style={{ fontSize: 34, fontWeight: 600, color: result.match ? "var(--accent)" : "var(--muted)" }}>{pct(result.confidence)}</div>
          <div>
            <div style={{ fontWeight: 600, fontSize: 16 }}>{result.match ? "Это один человек" : "Это разные люди"}</div>
            <div className="small muted">Порог совпадения: {pct(result.threshold)}</div>
          </div>
        </div>
      )}
    </div>
  );
}
