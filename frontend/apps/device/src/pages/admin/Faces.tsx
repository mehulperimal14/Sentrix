import { ApiError } from '@sentrix/api';
import { Button, EmptyState, Field, Spinner, Strip, TextInput, useToast } from '@sentrix/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type ChangeEvent } from 'react';
import { api } from '../../lib/api';
import { Panel } from './ui';

/** Faces of people who live here: enrolled from this camera or from photos.
 *  Faces never leave the device. */
export default function Faces() {
  const qc = useQueryClient();
  const toast = useToast();
  const [person, setPerson] = useState('');
  const [capturing, setCapturing] = useState(false);
  const [tick, setTick] = useState(0);
  const [confirm, setConfirm] = useState<string | null>(null);
  const q = useQuery({ queryKey: ['faces'], queryFn: () => api.get<any>('/api/authorized') });
  useEffect(() => {
    if (!capturing) return;
    const t = setInterval(() => setTick((x) => x + 1), 700);
    return () => clearInterval(t);
  }, [capturing]);
  const refresh = () => setTimeout(() => qc.invalidateQueries({ queryKey: ['faces'] }), 1500);
  const capture = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.set('person', person.trim());
      return api.post<any>('/api/authorized/capture', form);
    },
    onSuccess: (r) => {
      toast(r?.message ?? `Added a photo of ${person}. Capture another angle.`);
      refresh();
    },
    onError: (e) => toast(e instanceof ApiError ? `Not added: ${e.message}` : 'Not added.'),
  });
  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      const results: string[] = [];
      for (const f of files) {
        const form = new FormData();
        form.set('file', f);
        if (person.trim()) form.set('person', person.trim());
        try {
          await api.post('/authorized/upload', form);
          results.push(`${f.name}: added`);
        } catch (e) {
          results.push(`${f.name}: ${e instanceof ApiError ? e.message : 'not added'}`);
        }
      }
      return results;
    },
    onSuccess: (r) => {
      toast(r.join('. '));
      refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (filename: string) => api.del(`/api/authorized/${encodeURIComponent(filename)}`),
    onSuccess: () => {
      setConfirm(null);
      toast('Removed.');
      refresh();
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : 'Could not remove it.'),
  });

  if (q.isLoading) return <Spinner />;
  const d = q.data ?? {};
  const items: any[] = d.items ?? [];
  const byPerson = items.reduce<Record<string, any[]>>((acc, it) => {
    (acc[it.person] ||= []).push(it);
    return acc;
  }, {});
  const onFiles = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (files.length) upload.mutate(files);
    e.target.value = '';
  };
  return (
    <div className="grid gap-4">
      <h1 className="text-[24px] font-bold">Faces</h1>
      {d.available ? (
        <Strip tone="ok">Face recognition: {d.backend}. Faces stay on this device. Children’s faces are never added.</Strip>
      ) : (
        <Strip tone="warn">Face recognition is not available, so nobody is treated as recognised.</Strip>
      )}
      <div className="grid gap-4 lg:grid-cols-[1fr_1.3fr]">
        <Panel title="Add a person">
          <Field label="Name" htmlFor="face-name" help="One face, facing the camera, in good light. Add 2 or 3 photos from slightly different angles.">
            <TextInput id="face-name" value={person} onChange={(e) => setPerson(e.target.value)} placeholder="Dev" />
          </Field>
          {capturing ? <img src={`/api/authorized/preview.jpg?t=${tick}`} alt="Camera preview for adding a face" className="w-full rounded-xl bg-hatch" /> : null}
          <div className="grid gap-2 sm:grid-cols-2">
            <Button onClick={() => setCapturing((c) => !c)}>{capturing ? 'Close the camera' : 'Use the camera'}</Button>
            <Button variant="primary" disabled={!person.trim() || !capturing} busy={capture.isPending} onClick={() => capture.mutate()}>
              Capture and add
            </Button>
          </div>
          <label className="grid cursor-pointer place-items-center rounded-xl border-[1.5px] border-dashed border-line px-3 py-3 text-[14px] font-bold">
            Upload photos
            <input type="file" accept="image/jpeg,image/png" multiple className="sr-only" onChange={onFiles} />
          </label>
        </Panel>
        <Panel title="People this device recognises">
          {items.length === 0 ? (
            <EmptyState title="Nobody added yet">Until faces are added, everyone in view counts as someone we don’t recognise.</EmptyState>
          ) : (
            <div className="grid gap-3">
              {Object.entries(byPerson).map(([name, photos]) => (
                <div key={name} className="grid gap-2">
                  <b className="text-[14px]">{name}</b>
                  <div className="flex flex-wrap gap-2">
                    {photos.map((p) => (
                      <figure key={p.filename} className="grid w-28 gap-1 text-[11.5px]">
                        <img src={p.url} alt={`${name}, ${p.filename}`} loading="lazy" className="aspect-square w-28 rounded-lg bg-hatch object-cover" />
                        <span className={p.enrolled ? 'text-neem' : 'text-chilli'}>{p.enrolled ? 'Recognisable' : 'No usable face'}</span>
                        <button type="button" className="justify-self-start text-chilli underline" onClick={() => (confirm === p.filename ? remove.mutate(p.filename) : setConfirm(p.filename))}>
                          {confirm === p.filename ? 'Confirm remove' : 'Remove'}
                        </button>
                      </figure>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
