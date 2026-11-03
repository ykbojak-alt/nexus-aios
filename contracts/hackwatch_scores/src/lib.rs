//! HackWatch Scores — публичный реестр оценок заявок хакатона (NEAR Testnet).
//!
//! Жизненный цикл:
//!   1. owner (деплоер) пишет оценку проекта: set_score(project_id, score, note).
//!   2. Любой читает: get_score(project_id) -> Option<ScoreEntry>.
//!   3. get_all() — весь рейтинг (итерация по LookupMap).
//!
//! Только owner может писать: от сторонних аккаунтов — отказ (требование
//! «только owner» из плана второй заявки). Оценка 0..=100.

use near_sdk::borsh::{BorshDeserialize, BorshSerialize};
use near_sdk::store::LookupMap;
use near_sdk::{env, near, near_bindgen, require, AccountId, PanicOnDefault};

#[derive(BorshDeserialize, BorshSerialize, Clone, serde::Serialize, serde::Deserialize)]
#[borsh(crate = "near_sdk::borsh")]
pub struct ScoreEntry {
    pub score: u8,
    pub note: String,
    pub ts_ms: u64,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct HackwatchScores {
    owner: AccountId,
    scores: LookupMap<String, ScoreEntry>,
    order: Vec<String>,
}

#[near_bindgen]
impl HackwatchScores {
    #[init]
    pub fn new() -> Self {
        Self {
            owner: env::predecessor_account_id(),
            scores: LookupMap::new(b"s"),
            order: Vec::new(),
        }
    }

    /// Только owner: записать оценку проекта (0..=100) с заметкой.
    pub fn set_score(&mut self, project_id: String, score: u8, note: String) {
        require!(env::predecessor_account_id() == self.owner, "только owner пишет оценки");
        require!(score <= 100, "оценка вне диапазона 0..=100");
        require!(!project_id.trim().is_empty(), "project_id пуст");
        let is_new = self.scores.get(&project_id).is_none();
        self.scores.insert(project_id.clone(), ScoreEntry { score, note, ts_ms: env::block_timestamp_ms() });
        if is_new {
            self.order.push(project_id);
        }
        env::log_str("score_set");
    }

    /// Оценка проекта, если записана.
    pub fn get_score(&self, project_id: String) -> Option<ScoreEntry> {
        self.scores.get(&project_id).cloned()
    }

    /// Весь рейтинг как список (project_id, score, note, ts_ms).
    /// Неотсортирован: сортировку делает читатель/клиент.
    pub fn get_all(&self) -> Vec<(String, ScoreEntry)> {
        self.order.iter().filter_map(|k| {
            self.scores.get(k).cloned().map(|v| (k.clone(), v))
        }).collect()
    }
}

#[cfg(test)]
mod tests {
    use near_sdk::test_utils::VMContextBuilder;
    use near_sdk::{testing_env, AccountId};

    use super::*;

    fn owner() -> AccountId {
        "owner.testnet".parse().unwrap()
    }

    fn stranger() -> AccountId {
        "stranger.testnet".parse().unwrap()
    }

    fn ctx(account: AccountId) -> VMContextBuilder {
        let mut b = VMContextBuilder::new();
        b.predecessor_account_id(account);
        b
    }

    #[test]
    fn owner_sets_and_reads_score() {
        testing_env!(ctx(owner()).build());
        let mut c = HackwatchScores::new();
        c.set_score("proj-1".into(), 9, "demo ok".into());

        let s = c.get_score("proj-1".into()).expect("есть оценка");
        assert_eq!(s.score, 9);
        assert_eq!(s.note, "demo ok");
    }

    #[test]
    fn get_all_returns_entries() {
        testing_env!(ctx(owner()).build());
        let mut c = HackwatchScores::new();
        c.set_score("p1".into(), 9, "a".into());
        c.set_score("p2".into(), 4, "b".into());

        let all = c.get_all();
        assert_eq!(all.len(), 2);
        let names: Vec<_> = all.iter().map(|(k, _)| k.as_str()).collect();
        assert!(names.contains(&"p1") && names.contains(&"p2"));
    }

    #[test]
    #[should_panic(expected = "только owner")]
    fn stranger_cannot_set_score() {
        testing_env!(ctx(owner()).build());
        let mut c = HackwatchScores::new();
        testing_env!(ctx(stranger()).build());
        c.set_score("p".into(), 5, "x".into());
    }

    #[test]
    #[should_panic(expected = "оценка вне диапазона")]
    fn score_out_of_range_rejected() {
        testing_env!(ctx(owner()).build());
        let mut c = HackwatchScores::new();
        c.set_score("p".into(), 101, "x".into());
    }

    #[test]
    fn missing_score_returns_none() {
        testing_env!(ctx(owner()).build());
        let c = HackwatchScores::new();
        assert!(c.get_score("no-such".into()).is_none());
    }
}
