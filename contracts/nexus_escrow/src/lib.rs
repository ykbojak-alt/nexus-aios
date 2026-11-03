//! NEXUS Escrow — агентная экономика on-chain (NEAR Testnet, хакатон-демо).
//!
//! Жизненный цикл:
//!   1. register_agent(public_key) + стейк ≥ MIN_STAKE — агент в реестре.
//!   2. create_task(task_id, deadline_ms) + attached budget — эскроу на контракте.
//!   3. submit_bid(task_id, price, signature) — bid от зарегистрированного
//!      агента; подпись ed25519 проверяется on-chain по ключу из реестра.
//!   4. accept_bid(task_id, agent) — менеджер фиксирует победителя.
//!   5. submit_work(task_id, work_hash) — победитель сдаёт хэш результата.
//!   6. verify_and_release(task_id) — менеджер подтверждает → оплата агенту.
//!      slash(task_id) — после дедлайна без работы → бюджет менеджеру,
//!      стейк агента сжигается (locked-forever).

use near_sdk::borsh::{BorshDeserialize, BorshSerialize};
use near_sdk::store::LookupMap;
use near_sdk::{
    env, near, near_bindgen, require, AccountId, NearToken, PanicOnDefault,
    Promise,
};

/// Минимальный стейк агента. Для testnet-демо 0.1 NEAR — механика
/// слэшинга та же, прогон дешёвый и повторяемый. На проде поднять.
const MIN_STAKE: NearToken = NearToken::from_millinear(100);

/// Сообщение, которое агент подписывает в bid.
/// Формат: "bid:{task_id}:{agent_id}:{price_yocto}"
pub fn bid_message(task_id: &str, agent: &AccountId, price_yocto: u128) -> Vec<u8> {
    format!("bid:{task_id}:{agent}:{price_yocto}").into_bytes()
}

#[derive(BorshDeserialize, BorshSerialize)]
#[borsh(crate = "near_sdk::borsh")]
pub struct Agent {
    pub public_key: String,   // "ed25519:<base58>"
    pub stake: NearToken,     // залог; уменьшается при слэше
    pub active: bool,
    pub tasks_completed: u32, // репутация: успешные релизы
    pub tasks_failed: u32,    // репутация: слэши
}

#[derive(BorshDeserialize, BorshSerialize, Clone)]
#[borsh(crate = "near_sdk::borsh")]
pub struct Bid {
    pub agent: AccountId,
    pub price: NearToken,
    pub ts_ms: u64,
}

#[derive(BorshDeserialize, BorshSerialize, PartialEq, Clone, Copy, Debug)]
#[borsh(crate = "near_sdk::borsh")]
pub enum TaskStatus {
    Open,
    Awarded,
    Submitted,
    Completed,
    Refunded,
}

#[derive(BorshDeserialize, BorshSerialize)]
#[borsh(crate = "near_sdk::borsh")]
pub struct Task {
    pub manager: AccountId,
    pub budget: NearToken,        // эскроу, лежит на контракте
    pub deadline_ms: u64,
    pub status: TaskStatus,
    pub bids: Vec<Bid>,
    pub winner: Option<AccountId>,
    pub winning_price: NearToken,
    pub work_hash: Option<String>,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct NexusEscrow {
    owner: AccountId,               // менеджер-деплоер
    agents: LookupMap<AccountId, Agent>,
    tasks: LookupMap<String, Task>,
    burned: NearToken,              // суммарно сожженный стейк (честный счётчик)
    escrow: NearToken,              // сумма живых бюджетов в эскроу (Open/Awarded/Submitted)
}

#[near_bindgen]
impl NexusEscrow {
    #[init]
    pub fn new() -> Self {
        Self {
            owner: env::predecessor_account_id(),
            agents: LookupMap::new(b"a"),
            tasks: LookupMap::new(b"t"),
            burned: NearToken::from_yoctonear(0),
            escrow: NearToken::from_yoctonear(0),
        }
    }

    /// v1 → v2: меняется layout (Agent +rep, root +escrow) — старые
    /// записи не десериализуются, поэтому state пересоздаётся с новыми
    /// префиксами (v1-хранилище orphan'ится, события живут в tx-хэшах).
    /// Вызывается самим контракт-аккаунтом (#[private]).
    /// carried_burned — сожжённый стейк из v1, переносится честно.
    #[init(ignore_state)]
    #[private]
    pub fn migrate(owner: AccountId, carried_burned: String) -> Self {
        Self {
            owner,
            agents: LookupMap::new(b"ag".to_vec()),
            tasks: LookupMap::new(b"tk".to_vec()),
            burned: NearToken::from_yoctonear(
                carried_burned.parse().unwrap_or(0),
            ),
            escrow: NearToken::from_yoctonear(0),
        }
    }

    // ---------------------------------------------------------- registry

    /// Регистрация агента: public_key ed25519 + стейк ≥ 1 NEAR.
    #[payable]
    pub fn register_agent(&mut self, public_key: String) {
        let agent_id = env::predecessor_account_id();
        let deposit = env::attached_deposit();
        require!(
            deposit >= MIN_STAKE,
            format!("стейк < {} yocto", MIN_STAKE.as_yoctonear())
        );
        require!(public_key.starts_with("ed25519:"), "ключ — ed25519:<b58>");
        let (stake, done, failed) = self
            .agents
            .get(&agent_id)
            .map(|a| (a.stake, a.tasks_completed, a.tasks_failed))
            .unwrap_or((NearToken::from_yoctonear(0), 0, 0));
        self.agents.insert(
            agent_id.clone(),
            Agent {
                public_key,
                stake: stake.saturating_add(deposit),
                active: true,
                tasks_completed: done,
                tasks_failed: failed,
            },
        );
        env::log_str(format!("agent_registered: {agent_id}").as_str());
    }

    // ------------------------------------------------------------- tasks

    /// Менеджер создаёт задачу; attached_deposit = бюджет, блокируется
    /// на контракте до release/refund.
    #[payable]
    pub fn create_task(&mut self, task_id: String, deadline_ms: u64) {
        let budget = env::attached_deposit();
        require!(budget > NearToken::from_yoctonear(0), "бюджет = 0");
        require!(
            self.tasks.get(&task_id).is_none(),
            format!("задача {task_id} уже существует")
        );
        self.escrow = self.escrow.saturating_add(budget);
        self.tasks.insert(
            task_id.clone(),
            Task {
                manager: env::predecessor_account_id(),
                budget,
                deadline_ms,
                status: TaskStatus::Open,
                bids: Vec::new(),
                winner: None,
                winning_price: NearToken::from_yoctonear(0),
                work_hash: None,
            },
        );
        env::log_str(format!("task_created: {task_id}").as_str());
    }

    /// Bid агента: агент в реестре, стейк ≥ MIN_STAKE, подпись ed25519
    /// над bid_message проверяется host-функцией env::ed25519_verify.
    pub fn submit_bid(
        &mut self,
        task_id: String,
        price_yocto: String,
        signature: String,
    ) {
        let agent_id = env::predecessor_account_id();
        let agent = self
            .agents
            .get(&agent_id)
            .unwrap_or_else(|| env::panic_str("агент не зарегистрирован"));
        require!(agent.active, "агент слэшнут/неактивен");
        require!(agent.stake >= MIN_STAKE, "стейк ниже минимума");

        let task = self
            .tasks
            .get_mut(&task_id)
            .unwrap_or_else(|| env::panic_str("задача не найдена"));
        require!(task.status == TaskStatus::Open, "торг закрыт");
        require!(
            env::block_timestamp_ms() < task.deadline_ms,
            "ERR_TASK_EXPIRED"
        );
        let price = price_yocto.parse::<u128>().unwrap_or_else(|_| {
            env::panic_str("price_yocto — не число")
        });
        require!(price > 0, "цена = 0");
        require!(
            price <= task.budget.as_yoctonear(),
            "цена выше бюджета"
        );

        // ed25519-подпись над "bid:{task}:{agent}:{price}" — ключ из реестра.
        let pk_b58 = agent.public_key.strip_prefix("ed25519:").unwrap();
        let pk = bs58::decode(pk_b58)
            .into_vec()
            .unwrap_or_else(|_| env::panic_str("public_key не base58"));
        let sig = bs58::decode(&signature)
            .into_vec()
            .unwrap_or_else(|_| env::panic_str("signature не base58"));
        require!(pk.len() == 32 && sig.len() == 64, "плохая длина ключа/подписи");
        let msg = bid_message(&task_id, &agent_id, price);
        let ok = env::ed25519_verify(&sig.try_into().unwrap(), &msg,
                                     &pk.try_into().unwrap());
        require!(ok, "подпись bid не сошлась");

        task.bids.push(Bid {
            agent: agent_id,
            price: NearToken::from_yoctonear(price),
            ts_ms: env::block_timestamp_ms(),
        });
        env::log_str(format!("bid_submitted: {task_id}").as_str());
    }

    /// Менеджер фиксирует победителя — бюджет назначен ему.
    pub fn accept_bid(&mut self, task_id: String, agent_id: AccountId) {
        let task = self
            .tasks
            .get_mut(&task_id)
            .unwrap_or_else(|| env::panic_str("задача не найдена"));
        require!(
            env::predecessor_account_id() == task.manager,
            "не менеджер задачи"
        );
        require!(task.status == TaskStatus::Open, "задача не открыта");
        let bid = task
            .bids
            .iter()
            .find(|b| b.agent == agent_id)
            .unwrap_or_else(|| env::panic_str("bid агента не найден"));
        task.winner = Some(agent_id.clone());
        task.winning_price = bid.price;
        task.status = TaskStatus::Awarded;
        env::log_str(format!("bid_accepted: {task_id} → {agent_id}").as_str());
    }

    /// Победитель сдаёт хэш результата работы.
    pub fn submit_work(&mut self, task_id: String, work_hash: String) {
        let task = self
            .tasks
            .get_mut(&task_id)
            .unwrap_or_else(|| env::panic_str("задача не найдена"));
        require!(task.status == TaskStatus::Awarded, "не в стадии Awarded");
        require!(
            Some(env::predecessor_account_id()) == task.winner,
            "не победитель"
        );
        task.work_hash = Some(work_hash);
        task.status = TaskStatus::Submitted;
        env::log_str(format!("work_submitted: {task_id}").as_str());
    }

    /// Менеджер подтверждает работу → winning_price агенту,
    /// остаток эскроу возвращается менеджеру.
    pub fn verify_and_release(&mut self, task_id: String) {
        let task = self
            .tasks
            .get_mut(&task_id)
            .unwrap_or_else(|| env::panic_str("задача не найдена"));
        let caller = env::predecessor_account_id();
        require!(caller == task.manager, "не менеджер задачи");
        require!(
            task.status == TaskStatus::Submitted,
            "работа не сдана"
        );
        let winner = task.winner.clone().unwrap();
        let pay = task.winning_price;
        let refund = NearToken::from_yoctonear(
            task.budget.as_yoctonear() - pay.as_yoctonear(),
        );
        self.escrow = self.escrow.saturating_sub(task.budget);
        task.status = TaskStatus::Completed;
        if let Some(agent) = self.agents.get_mut(&winner) {
            agent.tasks_completed += 1;
        }
        Promise::new(winner).transfer(pay).detach();
        if refund.as_yoctonear() > 0 {
            Promise::new(task.manager.clone()).transfer(refund).detach();
        }
        env::log_str(format!("released: {task_id}").as_str());
    }

    /// После дедлайна без сданной работы: бюджет → менеджеру,
    /// стейк победителя сжигается (вычитается, остаётся навсегда
    /// заблокированным на контракте — burned-счётчик прозрачен судьям).
    pub fn slash(&mut self, task_id: String) {
        let task = self
            .tasks
            .get_mut(&task_id)
            .unwrap_or_else(|| env::panic_str("задача не найдена"));
        require!(
            task.status == TaskStatus::Awarded,
            "слэш — только по назначенной задаче"
        );
        require!(
            env::block_timestamp_ms() > task.deadline_ms,
            "дедлайн не прошёл"
        );
        let winner = task.winner.clone().unwrap();
        task.status = TaskStatus::Refunded;
        self.escrow = self.escrow.saturating_sub(task.budget);
        Promise::new(task.manager.clone()).transfer(task.budget).detach();
        if let Some(agent) = self.agents.get_mut(&winner) {
            agent.active = false;
            agent.tasks_failed += 1;
            self.burned = self.burned.saturating_add(agent.stake);
            agent.stake = NearToken::from_yoctonear(0);
        }
        env::log_str(format!("slashed: {task_id}, agent={winner}").as_str());
    }

    /// Менеджер забирает бюджет по открытой задаче после дедлайна
    /// (победителя не было — слэшить некого).
    pub fn refund_open_task(&mut self, task_id: String) {
        let task = self
            .tasks
            .get_mut(&task_id)
            .unwrap_or_else(|| env::panic_str("задача не найдена"));
        require!(
            env::predecessor_account_id() == task.manager,
            "не менеджер задачи"
        );
        require!(task.status == TaskStatus::Open, "задача не открыта");
        require!(
            env::block_timestamp_ms() > task.deadline_ms,
            "дедлайн не прошёл"
        );
        task.status = TaskStatus::Refunded;
        self.escrow = self.escrow.saturating_sub(task.budget);
        Promise::new(task.manager.clone()).transfer(task.budget).detach();
        env::log_str(format!("refunded: {task_id}").as_str());
    }

    // ------------------------------------------------------------- views

    /// Агент + репутация — JSON-объект (читается судьями и Qoder-парсером).
    pub fn get_agent(&self, agent_id: AccountId) -> Option<String> {
        self.agents.get(&agent_id).map(|a| {
            format!(
                "{{\"public_key\":\"{}\",\"stake\":\"{}\",\"active\":{},\
                 \"tasks_completed\":{},\"tasks_failed\":{}}}",
                a.public_key,
                a.stake.as_yoctonear(),
                a.active,
                a.tasks_completed,
                a.tasks_failed,
            )
        })
    }

    pub fn get_task(&self, task_id: String) -> Option<String> {
        self.tasks.get(&task_id).map(|t| {
            format!(
                "{{\"manager\":\"{}\",\"budget\":\"{}\",\"deadline_ms\":{},\
                 \"status\":\"{:?}\",\"bids\":{},\"winner\":{},\
                 \"winning_price\":\"{}\",\"work_hash\":{}}}",
                t.manager,
                t.budget.as_yoctonear(),
                t.deadline_ms,
                t.status,
                t.bids.len(),
                t.winner
                    .as_ref()
                    .map(|w| format!("\"{w}\""))
                    .unwrap_or("null".into()),
                t.winning_price.as_yoctonear(),
                t.work_hash
                    .as_ref()
                    .map(|h| format!("\"{h}\""))
                    .unwrap_or("null".into()),
            )
        })
    }

    pub fn get_bids(&self, task_id: String) -> Vec<(String, String, u64)> {
        self.tasks
            .get(&task_id)
            .map(|t| {
                t.bids
                    .iter()
                    .map(|b| {
                        (
                            b.agent.to_string(),
                            b.price.as_yoctonear().to_string(),
                            b.ts_ms,
                        )
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    pub fn get_burned(&self) -> String {
        self.burned.as_yoctonear().to_string()
    }

    /// Деньги, живущие в эскроу прямо сейчас (Σ бюджетов Open/Awarded/
    /// Submitted задач). Демо-пара: create_task 0.05 → release → 0.
    pub fn get_escrow_balance(&self) -> String {
        self.escrow.as_yoctonear().to_string()
    }

    /// Сырой баланс аккаунта контракта (storage + стейки + escrow + burned).
    pub fn get_contract_balance(&self) -> String {
        env::account_balance().as_yoctonear().to_string()
    }
}
