import { useState, useEffect } from 'react';
import { doc, onSnapshot } from 'firebase/firestore';
import { db } from '../firebase';
import { ensureCredits } from '../api';
import { useAuth } from './useAuth.tsx';

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
}

export function useCredits(): UserCredits {
    const { user } = useAuth();
    const [credits, setCredits] = useState<Omit<UserCredits, 'loading'>>({
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
    });
    const [loading, setLoading] = useState(true);
    const [ensureDone, setEnsureDone] = useState(false);
    const [hasSnapshot, setHasSnapshot] = useState(false);
    const [hasCreditsDoc, setHasCreditsDoc] = useState(false);

    useEffect(() => {
        if (!user) {
            setLoading(false);
            return;
        }
        setLoading(true);
        setEnsureDone(false);
        setHasSnapshot(false);
        setHasCreditsDoc(false);

        const ensureCreditsOnce = async () => {
            try {
                await ensureCredits();
            } catch (error) {
                console.error("Error ensuring credits:", error);
            } finally {
                setEnsureDone(true);
            }
        };

        ensureCreditsOnce();

        const userDocRef = doc(db, 'users', user.uid);

        const unsubscribe = onSnapshot(userDocRef, (snapshot) => {
            const docExists = snapshot.exists();
            setHasCreditsDoc(docExists);
            if (snapshot.exists()) {
                const data = snapshot.data();
                const creditsData = data?.credits || {};
                const topupCreditsData = data?.topupCredits || {};

                const balance = creditsData.balance || 0;
                const reserved = creditsData.reserved || 0;
                const topupTotalRemaining = Number(topupCreditsData.totalRemaining || 0);
                const topupReserved = Number(topupCreditsData.totalReserved || 0);
                const topupAvailable = Number(
                    topupCreditsData.totalAvailable ?? Math.max(0, topupTotalRemaining - topupReserved)
                );
                const subscriptionAvailable = balance - reserved;
                const available = subscriptionAvailable + topupAvailable;
                const expiresAt = creditsData.expiresAt?.toDate() || null;
                const topupEarliestExpiresAt = topupCreditsData.earliestExpiresAt?.toDate() || null;
                const overdrafted = creditsData.overdrafted || false;
                const isExpired = expiresAt ? new Date() > expiresAt : false;

                setCredits({
                    balance,
                    reserved,
                    available,
                    subscriptionAvailable,
                    topupAvailable,
                    topupActivePackCount: Number(topupCreditsData.activePackCount || 0),
                    topupEarliestExpiresAt,
                    expiresAt,
                    overdrafted,
                    isExpired,
                });
            }
            setHasSnapshot(true);
        }, (error) => {
            console.error("Error listening to credits:", error);
            setHasSnapshot(true);
            setLoading(false);
        });

        return () => unsubscribe();
    }, [user]);

    useEffect(() => {
        if (!hasSnapshot) {
            return;
        }
        if (!hasCreditsDoc) {
            if (ensureDone) {
                setLoading(false);
            }
            return;
        }
        const creditsLocked = credits.overdrafted || credits.isExpired || credits.available <= 0;
        if (!creditsLocked || ensureDone) {
            setLoading(false);
        }
    }, [credits.available, credits.isExpired, credits.overdrafted, ensureDone, hasSnapshot, hasCreditsDoc]);

    return { ...credits, loading };
}
