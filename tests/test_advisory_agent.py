import json
from agents.advisory_agent import advise
from agents.knowledge_store import KnowledgeStore
from agents.learning_models import KnowledgeEntry, KnowledgeStatus

def test_missing_knowledge_is_safe_and_advisory(tmp_path):
 r=advise(870,knowledge_path=tmp_path/'missing.jsonl',audit_path=tmp_path/'audit.jsonl'); assert r['decision']=='INSUFFICIENT_DATA' and r['advisory_only'] is True
def test_rejected_knowledge_ignored(tmp_path):
 p=tmp_path/'k.jsonl'; KnowledgeStore(p).append(KnowledgeEntry(id='bad',category='pattern_validation',title='bad',source='x',learning='x',status=KnowledgeStatus.REJECTED)); r=advise(870,knowledge_path=p,audit_path=tmp_path/'a'); assert 'bad' not in r['knowledge_ids_used']
def test_validated_knowledge_does_not_execute(tmp_path):
 p=tmp_path/'k.jsonl'; KnowledgeStore(p).append(KnowledgeEntry(id='good',category='pattern_validation',title='good',source='x',learning='x',status=KnowledgeStatus.VALIDATED), approved=True); r=advise(870,knowledge_path=p,audit_path=tmp_path/'a'); assert r['decision']=='REVIEW' and r['advisory_only'] and r['knowledge_ids_used']==['good']
def test_logging_failure_does_not_change_result(tmp_path):
 r=advise(870,audit_path=tmp_path/'missing'/'audit.jsonl'); assert r['advisory_only'] is True
