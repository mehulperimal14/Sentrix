import { ApiError } from '@sentrix/api';
import { Button, Field, Sheet, Spinner, Strip, TextInput, useToast } from '@sentrix/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Camera, Trash2, Upload } from 'lucide-react';
import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { Link } from 'react-router';
import { deviceApi } from '../../lib/api';
import { useStepUp } from '../../lib/stepup';

/** GET /api/authorized on the device (sentrix/web/api_models.py FaceGallery). */
interface FaceItem {
  filename: string;
  person: string;
  enrolled: boolean;
  url: string;
}
interface FaceGallery {
  backend: string | null;
  available: boolean;
  items: FaceItem[];
}

function reason(err: unknown): string {
  return err instanceof ApiError ? err.message : 'That did not work. Try again.';
}

/** The faces a device recognises, read and edited through the Portal tunnel.
 *  Photos go straight to the device and are stored only there (D12). Once a
 *  face is known, the device also learns that person's body look each time
 *  it sees the face (D24), so nobody uploads body photos. */
export function FacesSection({ deviceId, deviceName, online, owner }: { deviceId: string; deviceName: string; online: boolean; owner: boolean }) {
  const dapi = deviceApi(deviceId);
  const qc = useQueryClient();
  const toast = useToast();
  const stepUp = useStepUp();
  const key = ['faces', deviceId];
  const q = useQuery({ queryKey: key, queryFn: () => dapi.get<FaceGallery>('/api/authorized'), enabled: online, retry: false });
  const [person, setPerson] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<string[]>([]);
  const [capturing, setCapturing] = useState(false);
  const [removing, setRemoving] = useState<FaceItem | null>(null);
  const files = useRef<HTMLInputElement>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: key });
  const name = person.trim();

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(e.target.files ?? []);
    e.target.value = '';
    if (!picked.length || !name) return;
    setBusy('upload');
    const out: string[] = [];
    for (const f of picked) {
      const form = new FormData();
      form.set('file', f);
      form.set('person', name);
      try {
        await dapi.post('/api/authorized/upload', form);
        out.push(`${f.name}: added`);
      } catch (err) {
        out.push(`${f.name}: not added. ${reason(err)}`);
      }
    }
    setResults(out);
    setBusy(null);
    refresh();
  }

  async function capture() {
    setBusy('capture');
    setResults([]);
    const form = new FormData();
    form.set('person', name);
    try {
      await dapi.post('/api/authorized/capture', form);
      setResults([`Added a photo of ${name} from the camera. Add another from a slightly different angle.`]);
      refresh();
    } catch (err) {
      setResults([`Not added. ${reason(err)}`]);
    } finally {
      setBusy(null);
    }
  }

  async function remove(item: FaceItem) {
    setBusy('remove');
    try {
      await stepUp(() => dapi.del(`/api/authorized/${encodeURIComponent(item.filename)}`));
      toast(`Removed a photo of ${item.person}.`);
      setRemoving(null);
      refresh();
    } catch (err) {
      if (!(err instanceof ApiError && err.code === 'step_up_cancelled')) toast(reason(err));
    } finally {
      setBusy(null);
    }
  }

  const byPerson = (q.data?.items ?? []).reduce<Record<string, FaceItem[]>>((acc, it) => {
    (acc[it.person] ||= []).push(it);
    return acc;
  }, {});
  const consentMissing = q.error instanceof ApiError && q.error.code === 'consent_required';

  return (
    <section className="grid gap-2" aria-labelledby={`faces-${deviceId}`}>
      <h2 id={`faces-${deviceId}`} className="text-[16px] font-bold">
        Faces SENTRIX recognises
      </h2>
      <p className="-mt-1 max-w-prose text-[13px] text-slate">
        People whose face is here are not shown as “someone we don’t recognise”. Once SENTRIX knows a face, it also learns how that person looks from behind, so you only add face photos. Photos are stored only on the {deviceName}, never on the SENTRIX server.
      </p>

      {!online ? (
        <Strip tone="warn">The {deviceName} is offline. Faces can be seen and changed while it is online.</Strip>
      ) : q.isLoading ? (
        <Spinner />
      ) : consentMissing ? (
        <Strip tone="info">
          Face recognition is off for your home. <Link to="/settings/privacy" className="font-semibold underline">Turn it on in Privacy</Link> to add faces.
        </Strip>
      ) : q.isError ? (
        <Strip tone="alarm">{reason(q.error)}</Strip>
      ) : (
        <>
          {q.data && !q.data.available ? <Strip tone="warn">Face recognition is not working on the device right now. Photos are kept and used once it works again.</Strip> : null}
          {Object.keys(byPerson).length === 0 ? (
            <p className="text-[13px] text-slate">No faces yet. Until you add some, everyone in view counts as someone SENTRIX doesn’t recognise.</p>
          ) : (
            <ul className="grid gap-3">
              {Object.entries(byPerson).map(([who, photos]) => (
                <li key={who} className="grid gap-2 rounded-xl border border-line bg-paper p-3">
                  <b className="text-[14px]">{who}</b>
                  <div className="flex flex-wrap gap-2">
                    {photos.map((p) => (
                      <figure key={p.filename} className="grid w-24 gap-1">
                        <img src={p.url} alt={`Photo of ${p.person}`} loading="lazy" className="aspect-square w-24 rounded-lg bg-hatch object-cover" />
                        <figcaption className="flex items-center justify-between text-[11px]">
                          <span className={p.enrolled ? 'text-neem' : 'text-haldi'}>{p.enrolled ? 'In use' : 'No usable face'}</span>
                          {owner ? (
                            <button type="button" aria-label={`Remove this photo of ${p.person}`} onClick={() => setRemoving(p)} className="rounded p-0.5 text-slate hover:text-chilli">
                              <Trash2 aria-hidden className="size-3.5" />
                            </button>
                          ) : null}
                        </figcaption>
                      </figure>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {owner ? (
            <div className="grid gap-3 rounded-xl border border-line bg-paper p-3">
              <Field label="Name" htmlFor={`face-name-${deviceId}`} help="Adults who live here or visit often. Add 2 or 3 photos from slightly different angles, one face each, in good light. Never add children.">
                <TextInput id={`face-name-${deviceId}`} value={person} onChange={(e) => setPerson(e.target.value)} placeholder="Riya" autoComplete="off" />
              </Field>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" block={false} disabled={!name} busy={busy === 'upload'} icon={<Upload aria-hidden className="size-4" />} onClick={() => files.current?.click()}>
                  Upload photos
                </Button>
                <Button size="sm" block={false} disabled={!name} icon={<Camera aria-hidden className="size-4" />} onClick={() => setCapturing(true)}>
                  Take one with the SENTRIX camera
                </Button>
                <input ref={files} type="file" accept="image/jpeg,image/png" multiple hidden onChange={upload} />
              </div>
              {results.length ? (
                <ul className="grid gap-1 text-[13px]" role="status">
                  {results.map((r, i) => (
                    <li key={i} className={/not added/i.test(r) ? 'text-chilli' : 'text-neem'}>
                      {r}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : (
            <p className="text-[12.5px] text-slate">Only the account owner can add or remove faces.</p>
          )}
        </>
      )}

      <Sheet open={capturing} onClose={() => setCapturing(false)} title={`Take a photo of ${name || 'this person'}`}>
        {capturing ? <CapturePreview deviceId={deviceId} name={name} busy={busy === 'capture'} result={results[0]} onCapture={capture} onDone={() => setCapturing(false)} /> : null}
      </Sheet>
      <Sheet open={Boolean(removing)} onClose={() => setRemoving(null)} title="Remove this photo?">
        {removing ? (
          <div className="grid gap-3">
            <img src={removing.url} alt={`Photo of ${removing.person}`} className="w-40 rounded-lg" />
            <p className="text-[13.5px]">SENTRIX stops using this photo of {removing.person}. You will be asked for your password.</p>
            <Button variant="alarm" busy={busy === 'remove'} onClick={() => remove(removing)}>
              Remove the photo
            </Button>
            <Button variant="quiet" onClick={() => setRemoving(null)}>
              Keep it
            </Button>
          </div>
        ) : null}
      </Sheet>
    </section>
  );
}

/** The device camera's raw picture, refreshed about once a second, so the
 *  person can stand right before the photo is taken. */
function CapturePreview({ deviceId, name, busy, result, onCapture, onDone }: { deviceId: string; name: string; busy: boolean; result?: string; onCapture: () => void; onDone: () => void }) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1200);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="grid gap-3">
      <img src={`/d/${encodeURIComponent(deviceId)}/api/authorized/preview.jpg?t=${tick}`} alt="What the SENTRIX camera sees now" className="aspect-video w-full rounded-xl bg-hatch object-cover" />
      <p className="text-[13px] text-slate">{name} stands alone in front of the camera, facing it, close enough that the face is clear.</p>
      {result ? (
        <p role="status" className={/not added/i.test(result) ? 'text-[13px] font-semibold text-chilli' : 'text-[13px] font-semibold text-neem'}>
          {result}
        </p>
      ) : null}
      <Button variant="primary" busy={busy} onClick={onCapture}>
        Take the photo
      </Button>
      <Button variant="quiet" onClick={onDone}>
        Done
      </Button>
    </div>
  );
}
