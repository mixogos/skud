import { useEffect, useState } from "react";
import { get, post } from "./api";
import { Icon } from "./ui";

interface UpdateStatus {
  configured: boolean; current: string; latest?: string | null; update_available: boolean;
  notes?: string; error?: string; size?: number | null;
}

/** Баннер появляется сам, когда в GitHub вышла версия новее установленной. */
export function UpdateBanner() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    get<UpdateStatus>("/update/status/").then(setStatus).catch(() => {});
  }, []);

  if (!status || !status.update_available) return null;

  const update = async () => {
    if (!confirm(`Обновить программу до версии ${status.latest}?\n\nПрограмма закроется, установит обновление и запустится снова. Данные и настройки сохранятся.`)) return;
    setBusy(true);
    setMessage("Загрузка обновления…");
    try {
      await post("/update/apply/");
      setMessage("Установщик запущен — программа сейчас закроется и обновится.");
    } catch (e: any) {
      setMessage(e.message || "Не удалось запустить обновление");
      setBusy(false);
    }
  };

  return (
    <div className="update-banner" role="status">
      <Icon name="download" size={17} />
      <span className="grow">
        Доступна новая версия <strong>{status.latest}</strong> <span className="muted">(у вас {status.current})</span>
        {message && <> — {message}</>}
      </span>
      {!message && <button className="btn primary sm" disabled={busy} onClick={update}>Обновить</button>}
    </div>
  );
}
