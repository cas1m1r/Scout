from flask import Flask, render_template, request, jsonify, redirect, url_for
import asyncio
import json
import os
import glob
from datetime import datetime, timezone
import threading
import time
from collections import defaultdict

# Import your existing modules
from main import check_google_rss, save_raw_stories, ensure_topic_dir, load_pre_prompt, summarize_story, \
	extract_machine_json, extract_human_summary
from tracker import EventTracker, load_store
from llama_utils import setup_client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Change this in production

# Global variables to track running searches
active_searches = {}
search_results = {}


class SearchStatus:
	def __init__(self, topic):
		self.topic = topic
		self.status = "initializing"
		self.progress = 0
		self.total_articles = 0
		self.processed_articles = 0
		self.results = []
		self.error = None
		self.start_time = datetime.now()


async def run_news_search(topic, search_id):
	"""Run the news search process asynchronously"""
	try:
		status = active_searches[search_id]
		status.status = "fetching_news"
		
		# Get news from Google RSS
		protest_news = check_google_rss(topic, '24h')
		status.total_articles = len(protest_news)
		status.progress = 20
		
		if not protest_news:
			status.status = "completed"
			status.error = "No news articles found for this topic"
			return
		
		# Load system prompt
		system_prompt = load_pre_prompt()
		condensed = []
		summarizer = 'gemma3:4b'  # You might want to make this configurable
		
		status.status = "processing_articles"
		
		# Get URL from environment
		url = os.environ.get('URL')
		if not url:
			status.error = "URL environment variable not set"
			status.status = "error"
			return
		
		# Process each article
		for i, event in enumerate(protest_news):
			try:
				status.processed_articles = i + 1
				status.progress = 20 + (60 * (i + 1) / len(protest_news))
				
				llm = await summarize_story(url, event, summarizer, system_prompt)
				
				# Extract content from LLM response
				if hasattr(llm, "message"):
					raw_text = llm.message.content
				elif isinstance(llm, dict) and "message" in llm and "content" in llm["message"]:
					raw_text = llm["message"]["content"]
				else:
					raw_text = str(llm)
				
				data = extract_machine_json(raw_text)
				if not data:
					continue
				
				human = extract_human_summary(raw_text)
				
				organized = {
					'raw': event,
					'readable': human,
					'details': data
				}
				condensed.append(organized)
			
			except Exception as e:
				print(f"Error processing article {i}: {e}")
				continue
		
		status.progress = 85
		status.status = "analyzing_clusters"
		
		# Save results
		save_raw_stories(condensed, topic)
		
		# Process with tracker
		flat_stories = []
		for c in condensed:
			s = c['raw']
			flat_stories.append({
				"title": s.get("title", ""),
				"link": s.get("link", ""),
				"source": s.get("source", {}),
				"date": s.get("date", ""),
				"ts": s.get("ts", "")
			})
		
		tracker = EventTracker(cooldown_minutes=30, min_score=60)
		alerts, all_clusters = tracker.process(flat_stories)
		
		status.results = {
			'condensed': condensed,
			'alerts': alerts,
			'clusters': all_clusters
		}
		status.progress = 100
		status.status = "completed"
		
		# Store results for later retrieval
		search_results[search_id] = status.results
	
	except Exception as e:
		status.error = str(e)
		status.status = "error"
		print(f"Search error: {e}")


def run_search_sync(topic, search_id):
	"""Wrapper to run async search in a thread"""
	loop = asyncio.new_event_loop()
	asyncio.set_event_loop(loop)
	try:
		loop.run_until_complete(run_news_search(topic, search_id))
	finally:
		loop.close()


@app.route('/')
def index():
	return render_template('index.html')


@app.route('/start_search', methods=['POST'])
def start_search():
	topic = request.form.get('topic', '').strip()
	if not topic:
		return jsonify({'error': 'Topic is required'}), 400
	
	# Generate search ID
	search_id = f"search_{int(time.time())}_{hash(topic) % 10000}"
	
	# Initialize search status
	active_searches[search_id] = SearchStatus(topic)
	
	# Start search in background thread
	thread = threading.Thread(target=run_search_sync, args=(topic, search_id))
	thread.daemon = True
	thread.start()
	
	return jsonify({'search_id': search_id})


@app.route('/search_status/<search_id>')
def search_status(search_id):
	if search_id not in active_searches:
		return jsonify({'error': 'Search not found'}), 404
	
	status = active_searches[search_id]
	return jsonify({
		'status': status.status,
		'progress': status.progress,
		'total_articles': status.total_articles,
		'processed_articles': status.processed_articles,
		'error': status.error,
		'elapsed_time': (datetime.now() - status.start_time).total_seconds()
	})


@app.route('/search_results/<search_id>')
def get_search_results(search_id):
	if search_id not in search_results:
		return jsonify({'error': 'Results not found'}), 404
	
	return jsonify(search_results[search_id])


@app.route('/view_results/<search_id>')
def view_results(search_id):
	if search_id not in search_results:
		return render_template('error.html', error='Results not found')
	
	results = search_results[search_id]
	status = active_searches.get(search_id)
	topic = status.topic if status else 'Unknown'
	
	return render_template('results.html',
	                       results=results,
	                       topic=topic,
	                       search_id=search_id)


@app.route('/historical_data')
def historical_data():
	"""View historical data from stored files"""
	# Get all topic directories
	topics = []
	for item in os.listdir('.'):
		if os.path.isdir(item) and not item.startswith('.'):
			# Check if it has data files
			data_files = glob.glob(os.path.join(item, 'data_*.json'))
			cluster_files = glob.glob(os.path.join(item, 'clusters_*.json'))
			if data_files or cluster_files:
				topics.append({
					'name': item,
					'data_files': len(data_files),
					'cluster_files': len(cluster_files),
					'latest_data': max([os.path.getctime(f) for f in data_files] + [0]),
					'latest_cluster': max([os.path.getctime(f) for f in cluster_files] + [0])
				})
	
	# Sort by latest activity
	topics.sort(key=lambda x: max(x['latest_data'], x['latest_cluster']), reverse=True)
	
	return render_template('historical.html', topics=topics)


@app.route('/view_topic/<topic>')
def view_topic(topic):
	"""View data for a specific topic"""
	topic_dir = topic.lower().replace(' ', '')
	if not os.path.isdir(topic_dir):
		return render_template('error.html', error='Topic not found')
	
	# Get all data files
	data_files = glob.glob(os.path.join(topic_dir, 'data_*.json'))
	cluster_files = glob.glob(os.path.join(topic_dir, 'clusters_*.json'))
	
	# Sort by creation time (newest first)
	data_files.sort(key=os.path.getctime, reverse=True)
	cluster_files.sort(key=os.path.getctime, reverse=True)
	
	# Load latest data if available
	latest_data = None
	latest_clusters = None
	
	if data_files:
		with open(data_files[0], 'r', encoding='utf-8') as f:
			latest_data = json.load(f)
	
	if cluster_files:
		with open(cluster_files[0], 'r', encoding='utf-8') as f:
			latest_clusters = json.load(f)
	
	return render_template('topic_view.html',
	                       topic=topic,
	                       data_files=data_files,
	                       cluster_files=cluster_files,
	                       latest_data=latest_data,
	                       latest_clusters=latest_clusters)


@app.route('/event_store')
def view_event_store():
	"""View the event store (tracker history)"""
	events = load_store()
	
	# Group by cluster_id and get latest for each
	clusters = defaultdict(list)
	for event in events:
		if 'cluster_id' in event:
			clusters[event['cluster_id']].append(event)
	
	# Sort each cluster by run_at and get summary
	cluster_summaries = []
	for cluster_id, cluster_events in clusters.items():
		cluster_events.sort(key=lambda x: x.get('run_at', ''), reverse=True)
		latest = cluster_events[0]
		
		if 'summary' in latest:
			summary = latest['summary']
			summary['event_count'] = len(cluster_events)
			summary['last_alert'] = latest.get('alerted_at')
			summary['last_run'] = latest.get('run_at')
			cluster_summaries.append(summary)
	
	# Sort by score descending
	cluster_summaries.sort(key=lambda x: x.get('score', 0), reverse=True)
	
	return render_template('event_store.html', clusters=cluster_summaries)


if __name__ == '__main__':
	app.run(debug=True, host='0.0.0.0', port=5000)