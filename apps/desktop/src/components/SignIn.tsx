import { useState } from 'react';
import { LogIn, KeyRound, ArrowLeft } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import { supabaseConfigured, signInWithPassword, signUpWithPassword } from '@/lib/supabase';

interface Props {
  // Escape hatch: let the user configure the legacy shared key instead of
  // signing in (transitional — removed at the supervised Supabase cutover).
  onUseLegacyKey: () => void;
  legacyKeyEnabled?: boolean;
  // When the screen is reached while already (legacy-)authed, offer a way back
  // to the workspace. Omitted when SignIn is the hard auth gate (no way back).
  onBack?: () => void;
}

export function SignIn({ onUseLegacyKey, legacyKeyEnabled = true, onBack }: Props) {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!email.trim() || !password) {
      setErr('Email and password are required.');
      return;
    }
    setBusy(true);
    setErr(null);
    setInfo(null);
    const fn = mode === 'signin' ? signInWithPassword : signUpWithPassword;
    const { error } = await fn(email.trim(), password);
    setBusy(false);
    if (error) {
      setErr(error);
    } else if (mode === 'signup') {
      setInfo('Account created. Check your email if confirmation is required, then sign in.');
      setMode('signin');
    }
    // On successful sign-in the auth listener flips the app into the workspace.
  };

  return (
    <div className="mx-auto flex h-full w-full max-w-md flex-col justify-center p-8">
      {onBack && (
        <button
          onClick={onBack}
          className="mb-3 inline-flex items-center gap-1.5 self-start text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Back to workspace
        </button>
      )}
      <Card>
        <CardHeader>
          <CardTitle>{mode === 'signin' ? 'Sign in to AgentsHive' : 'Create your AgentsHive account'}</CardTitle>
          <CardDescription>
            {supabaseConfigured
              ? 'Your account scopes every project and mission to you. Agents you launch authenticate as you.'
              : 'Supabase is not configured in this build — use the legacy shared key below.'}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {supabaseConfigured && (
            <>
              <div className="space-y-1.5">
                <Label>Email</Label>
                <Input
                  type="email"
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => { setEmail(e.target.value); setErr(null); }}
                  onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Password</Label>
                <Input
                  type="password"
                  placeholder="••••••••"
                  value={password}
                  onChange={(e) => { setPassword(e.target.value); setErr(null); }}
                  onKeyDown={(e) => e.key === 'Enter' && submit()}
                />
              </div>
              {err && <p className="text-sm text-destructive">{err}</p>}
              {info && <p className="text-sm text-success">{info}</p>}
              <div className="flex items-center gap-3">
                <Button onClick={submit} disabled={busy}>
                  <LogIn className="h-4 w-4" />
                  {busy ? 'Working…' : mode === 'signin' ? 'Sign in' : 'Sign up'}
                </Button>
                <button
                  className="text-xs text-muted-foreground underline-offset-2 hover:underline"
                  onClick={() => { setMode(mode === 'signin' ? 'signup' : 'signin'); setErr(null); setInfo(null); }}
                >
                  {mode === 'signin' ? 'Need an account? Sign up' : 'Have an account? Sign in'}
                </button>
              </div>
              {legacyKeyEnabled && <Separator />}
            </>
          )}
          {legacyKeyEnabled && (
            <Button variant="ghost" size="sm" className="gap-2 text-muted-foreground" onClick={onUseLegacyKey}>
              <KeyRound className="h-3.5 w-3.5" />
              Use legacy shared key instead
            </Button>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
