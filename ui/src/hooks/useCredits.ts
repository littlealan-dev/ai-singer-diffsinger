import { useState, useEffect } from 'react';
import { doc, onSnapshot } from 'firebase/firestore';
import { db } from '../firebase';
import { ensureCredits, type CreditsBootstrapResponse } from '../api';
import { useAuth } from './useAuth.tsx';

export type CreditsStatus = 'signed_out' | 'loading' | 'ready' | 'error';

export interface UserCredits {
    balance: number;
    reserved: number;
    available: number;
    subscriptionAvailable: number;
    topupAvailable: number;
    topupActivePackCount: number;
    topupEarliestExpiresAt: Date | null;
    expiresAt: Date | null;
    overdrafted: boolean;
    isExpired: boolean;
    loading: boolean;
    status: CreditsStatus;
    error: string | null;
}

type CreditsData = Omit<UserCredits, 'loading' | 'status' | 'error'>;

const EMPTY_CREDITS: CreditsData = {
    balance: 0,
    reserved: 0,
    available: 0,
    subscriptionAvailable: 0,
    topupAvailable: 0,
    topupActivePackCount: 0,
    topupEarliestExpiresAt: null,
    expiresAt: null,
    overdrafted: false,
    isExpired: false,
};

export function useCredits(): UserCredits {
    const { user } = useAuth();
    const [credits, setCredits] = useState<CreditsData>(EMPTY_CREDITS);
    const [status, setStatus] = useState<CreditsStatus>('loading');
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!user) {
            setCredits(EMPTY_CREDITS);
            setStatus('signed_out');
            setError(null);
            return;
        }
        let active = true;
        setCredits(EMPTY_CREDITS);
        setStatus('loading');
        setError(null);

        const ensureCreditsOnce = async () => {
            try {
                const bootstrap = await ensureCredits();
                if (!active) return;
                // The API response is committed by the backend transaction, so it
                // is authoritative while the Firestore listener catches up.
                setCredits(creditsFromBootstrap(bootstrap));
                setStatus('ready');
            } catch (error) {
                console.error("Error ensuring credits:", error);
                if (active) {
                    setStatus('error');
                    setError('Could not load your credit balance.');
                }
            }
        };

        ensureCreditsOnce();

        const userDocRef = doc(db, 'users', user.uid);

        const unsubscribe = onSnapshot(userDocRef, (snapshot) => {
            if (!active || !snapshot.exists()) return;
            const snapshotCredits = creditsFromUserDocument(snapshot.data());
            if (!snapshotCredits) return;
            setCredits(snapshotCredits);
            setStatus('ready');
            setError(null);
        }, (error) => {
            console.error("Error listening to credits:", error);
            if (active) {
                setStatus('error');
                setError('Could not load your credit balance.');
            }
        });

        return () => {
            active = false;
            unsubscribe();
        };
    }, [user]);

    return { ...credits, loading: status === 'loading', status, error };
}

function creditsFromBootstrap(bootstrap: CreditsBootstrapResponse): CreditsData {
    const balance = finiteNumber(bootstrap.balance) ?? 0;
    const reserved = finiteNumber(bootstrap.reserved) ?? 0;
    const subscriptionAvailable = balance - reserved;
    const topupAvailable = Math.max(
        0,
        (finiteNumber(bootstrap.available) ?? subscriptionAvailable) - subscriptionAvailable
    );
    const expiresAt = dateFromUnknown(bootstrap.expires_at);
    return {
        balance,
        reserved,
        available: subscriptionAvailable + topupAvailable,
        subscriptionAvailable,
        topupAvailable,
        topupActivePackCount: 0,
        topupEarliestExpiresAt: null,
        expiresAt,
        overdrafted: Boolean(bootstrap.overdrafted),
        isExpired: Boolean(bootstrap.is_expired) || Boolean(expiresAt && new Date() > expiresAt),
    };
}

function creditsFromUserDocument(data: Record<string, unknown>): CreditsData | null {
    const creditsData = objectValue(data.credits);
    const billingData = objectValue(data.billing);
    // Documents written by DOI or checkout flows can exist before credit
    // bootstrap. They are not a legitimate zero-credit account state.
    if (!creditsData || !billingData || typeof billingData.activePlanKey !== 'string') {
        return null;
    }
    const balance = finiteNumber(creditsData.balance);
    if (balance === null) return null;
    const reserved = finiteNumber(creditsData.reserved) ?? 0;
    const topupCreditsData = objectValue(data.topupCredits) ?? {};
    const topupTotalRemaining = finiteNumber(topupCreditsData.totalRemaining) ?? 0;
    const topupReserved = finiteNumber(topupCreditsData.totalReserved) ?? 0;
    const topupAvailable =
        finiteNumber(topupCreditsData.totalAvailable) ??
        Math.max(0, topupTotalRemaining - topupReserved);
    const expiresAt = dateFromUnknown(creditsData.expiresAt);
    return {
        balance,
        reserved,
        available: balance - reserved + topupAvailable,
        subscriptionAvailable: balance - reserved,
        topupAvailable,
        topupActivePackCount: finiteNumber(topupCreditsData.activePackCount) ?? 0,
        topupEarliestExpiresAt: dateFromUnknown(topupCreditsData.earliestExpiresAt),
        expiresAt,
        overdrafted: Boolean(creditsData.overdrafted),
        isExpired: Boolean(expiresAt && new Date() > expiresAt),
    };
}

function objectValue(value: unknown): Record<string, unknown> | null {
    return value && typeof value === 'object' ? value as Record<string, unknown> : null;
}

function finiteNumber(value: unknown): number | null {
    if (typeof value !== 'number' && typeof value !== 'string') return null;
    const numberValue = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(numberValue) ? numberValue : null;
}

function dateFromUnknown(value: unknown): Date | null {
    if (!value) return null;
    if (value instanceof Date) return value;
    if (typeof value === 'string') {
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? null : date;
    }
    const timestamp = value as { toDate?: () => Date };
    return typeof timestamp.toDate === 'function' ? timestamp.toDate() : null;
}
