import { Button, ErrorState, Spinner } from '@sentrix/ui';
import { X } from 'lucide-react';
import { useNavigate, useParams } from 'react-router';
import { useHome } from '../../lib/data';

/** Live view through the Portal: the device shows that someone is watching. */
export function Live() {
  const { deviceId } = useParams();
  const navigate = useNavigate();
  const home = useHome();
  if (home.isLoading) return <Spinner />;
  const d = home.data?.devices.find((x) => x.id === deviceId);
  if (!d) return <div className="p-4"><ErrorState title="Device not found" /></div>;
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-[#0e1311] text-[#e3eae7]">
      <div className="flex items-center justify-between px-4 py-3 safe-top safe-x">
        <span className="flex items-center gap-2 text-[14px] font-semibold">
          <span className="rounded bg-chilli-fill px-1.5 text-[11px] font-bold text-on-fill">LIVE</span>
          {d.room ?? d.name}
        </span>
        <button type="button" onClick={() => navigate(-1)} className="inline-flex items-center gap-1 rounded-lg border border-[#3b4844] px-3 py-1.5 text-[13px] font-semibold">
          <X aria-hidden className="size-4" /> Close
        </button>
      </div>
      <div className="grid min-h-0 flex-1 place-items-center">
        {d.online ? <img src={`/d/${d.id}/video`} alt={`Live picture of the ${d.room ?? 'room'}`} className="max-h-full w-full object-contain" /> : <p>The device is offline, so there is no live picture.</p>}
      </div>
      <p className="px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-2 text-[12.5px] text-[#9daca6]">
        Video passes through the SENTRIX server and is not stored. The device screen shows that you are watching.
      </p>
      {!d.online ? <div className="p-4"><Button onClick={() => navigate('/home')}>Back to Home</Button></div> : null}
    </div>
  );
}
