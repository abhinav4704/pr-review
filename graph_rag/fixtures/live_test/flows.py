"""DFG fixtures — exercises data-flow summary and PASSES edge enrichment.

  echo(x)              -> dfg_returns_from_params = [0]
  DataStore.save(data) -> field write self.last_data <- param 0
  Handler.handle → FlowService.process → DataStore.save  (2-hop param chain)
"""


def echo(x):
    return x


class DataStore:
    def save(self, data):
        self.last_data = data
        return data


class FlowService:
    def process(self, data):
        store = DataStore()
        store.save(data)


class Handler:
    def handle(self, user_input):
        svc = FlowService()
        svc.process(user_input)
