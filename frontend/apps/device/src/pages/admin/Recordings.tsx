import { RecordingsPanel } from '@sentrix/ui';
import { api, useSession } from '../../lib/api';
import { Panel } from './ui';

export default function Recordings() {
  const { data } = useSession();
  return (
    <div className="grid max-w-2xl gap-4">
      <h1 className="text-[24px] font-bold">Record video</h1>
      <p className="text-[13.5px] text-slate">Training footage from this camera and microphone. Ask everyone in the room before you record.</p>
      <Panel title="Recordings">
        <RecordingsPanel client={api} timezone={data?.timezone ?? 'Asia/Kolkata'} />
      </Panel>
    </div>
  );
}
