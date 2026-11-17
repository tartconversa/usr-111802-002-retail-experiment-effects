"""门店实验的计划入组与活动登记。"""
class ExperimentRoster:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS assignments(experiment TEXT, store TEXT, arm TEXT, PRIMARY KEY(experiment,store))")

    def assign(self, experiment, store, arm):
        if arm not in {"trial", "control"}: raise ValueError("未知实验分组")
        prior = self.arm(experiment, store)
        if prior and prior != arm: raise ValueError("门店已分配另一实验组")
        self.connection.execute("INSERT OR IGNORE INTO assignments VALUES(?,?,?)", (experiment,store,arm))

    def arm(self, experiment, store):
        row = self.connection.execute("SELECT arm FROM assignments WHERE experiment=? AND store=?", (experiment,store)).fetchone()
        return row[0] if row else None
