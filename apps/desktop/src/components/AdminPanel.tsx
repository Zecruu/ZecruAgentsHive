import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, ShieldAlert, Ban, ShieldCheck, Trash2 } from 'lucide-react';
import { ah, type AdminUser } from '@/lib/agentshive';
import { getAccessToken } from '@/lib/supabase';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { cn } from '@/lib/utils';

const PLANS = ['free', 'pro', 'pro_unlimited'];

export function AdminPanel() {
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // sub currently acting on
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    setUsers(null);
    try {
      const token = getAccessToken();
      if (!token) throw new Error('not signed in');
      const r = await ah().admin.listUsers(token);
      setUsers(r.users || []);
    } catch (e: any) {
      setErr(e?.message || String(e));
      setUsers([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const act = async (sub: string, fn: () => Promise<unknown>) => {
    setBusy(sub);
    setErr(null);
    try {
      await fn();
      await load();
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setBusy(null);
      setConfirmRemove(null);
    }
  };

  const token = () => {
    const t = getAccessToken();
    if (!t) throw new Error('not signed in');
    return t;
  };

  return (
    <div className="h-full overflow-y-auto scrollbar-thin p-6">
      <div className="mx-auto max-w-6xl">
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <ShieldAlert className="h-5 w-5 text-accent" />
            <h2 className="text-lg font-semibold tracking-tight">Admin · all users</h2>
            <Badge variant="muted" className="normal-case">cross-tenant</Badge>
          </div>
          <Button variant="outline" size="sm" onClick={load}>
            <RefreshCw className="h-3.5 w-3.5" /> Refresh
          </Button>
        </div>

        {err && <p className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{err}</p>}
        {users === null && <p className="text-sm text-muted-foreground">Loading…</p>}
        {users && users.length === 0 && !err && <p className="text-sm text-muted-foreground">No users.</p>}

        {users && users.length > 0 && (
          <div className="overflow-x-auto rounded-lg border border-border/60 glass">
            <table className="w-full text-left text-[13px]">
              <thead className="border-b border-border/60 text-[11px] uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">User</th>
                  <th className="px-3 py-2">Plan</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Trial</th>
                  <th className="px-3 py-2">Projects</th>
                  <th className="px-3 py-2">Missions</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.sub} className={cn('border-b border-border/40 last:border-0', u.banned && 'opacity-60')}>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1.5 font-medium">
                        {u.email || '—'}
                        {u.role === 'admin' && <Badge variant="ok" className="text-[9px]">admin</Badge>}
                        {u.banned && <Badge variant="err" className="text-[9px]">banned</Badge>}
                      </div>
                      <code className="text-[10px] text-muted-foreground">{u.sub}</code>
                    </td>
                    <td className="px-3 py-2">
                      <Select
                        value={u.plan}
                        onValueChange={(v) => act(u.sub, () => ah().admin.setPlan(token(), u.sub, v))}
                      >
                        <SelectTrigger className="h-7 w-[140px] text-[12px]"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {PLANS.map((p) => <SelectItem key={p} value={p}>{p}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">{u.subscription_status}</td>
                    <td className="px-3 py-2 text-muted-foreground">{u.trial_reports_used}</td>
                    <td className="px-3 py-2 text-muted-foreground">{u.project_count}</td>
                    <td className="px-3 py-2 text-muted-foreground">{u.mission_count}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center justify-end gap-1.5">
                        {u.banned ? (
                          <Button variant="outline" size="sm" className="h-7 px-2 text-[11px]" disabled={busy === u.sub}
                            onClick={() => act(u.sub, () => ah().admin.setBanned(token(), u.sub, false))}>
                            <ShieldCheck className="h-3.5 w-3.5" /> Unban
                          </Button>
                        ) : (
                          <Button variant="outline" size="sm" className="h-7 px-2 text-[11px]" disabled={busy === u.sub}
                            onClick={() => act(u.sub, () => ah().admin.setBanned(token(), u.sub, true))}>
                            <Ban className="h-3.5 w-3.5" /> Ban
                          </Button>
                        )}
                        {confirmRemove === u.sub ? (
                          <Button variant="destructive" size="sm" className="h-7 px-2 text-[11px]" disabled={busy === u.sub}
                            onClick={() => act(u.sub, () => ah().admin.removeUser(token(), u.sub))}>
                            <Trash2 className="h-3.5 w-3.5" /> Confirm?
                          </Button>
                        ) : (
                          <Button variant="ghost" size="sm" className="h-7 px-2 text-[11px] text-muted-foreground hover:text-destructive"
                            onClick={() => setConfirmRemove(u.sub)}>
                            <Trash2 className="h-3.5 w-3.5" /> Remove
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-3 text-[11px] text-muted-foreground">
          Remove deletes the Supabase user and cascade-deletes only that user's data. Ban takes effect immediately (their agents are rejected).
        </p>
      </div>
    </div>
  );
}
