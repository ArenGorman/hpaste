import urllib2
import json
import copy

import re
import base64

from collectionbase import CollectionBase,CollectionItem,CollectionInconsistentError,CollectionSyncError,CollectionItemInvalidError,CollectionItemReadonlyError,CollectionReadonlyError
from logger import defaultLogger as log

currentVersion = (1, 1)

qtAvailable = False
try:
	from PySide2.QtGui import QPixmap
	from PySide2.QtCore import QBuffer, QByteArray
	qtAvailable = True
except ImportError:
	from PySide.QtGui import QPixmap
	from PySide.QtCore import QBuffer, QByteArray
	qtAvailable = True


from logger import defaultLogger as log


def urlopen_nt(req):
	code = -1
	rep = None
	try:
		rep = urllib2.urlopen(req)
	except urllib2.HTTPError as e:
		code = e.code
	except urllib2.URLError as e:
		raise CollectionSyncError('unable to reach collection: %s'%e.reason)

	if (code == -1): code = rep.getcode()
	return code, rep


class InvalidToken(CollectionSyncError):
	def __init__(self,message):
		super(InvalidToken,self).__init__(message)


class Cacher(object):
	def __init__(self):
		self.__cache = {}

	def __contains__(self, item):
		return item in self.__cache

	def __getitem__(self, item):
		return self.__cache[item]

	def __setitem__(self, key, value):
		self.__cache[key] = value

	def clear(self):
		self.__cache.clear()

globalIconCacher = Cacher()

class GithubCollection(CollectionBase):
	def __init__(self, token_or_username, public=False, token_for_public_access=None):
		assert isinstance(token_or_username,str) or isinstance(token_or_username,unicode), 'token must be a str'

		if(public):
			self.__token = None
			self.__headers = {'User-Agent': 'HPaste', 'Accept': 'application/vnd.github.v3+json'}
			if(token_for_public_access is not None):
				self.__headers['Authorization']='Token %s'%token_for_public_access
			self.__readonly = True
			self.__name=token_or_username
		else:
			self.__token=str(token_or_username)
			self.__headers = {'User-Agent': 'HPaste', 'Authorization':'Token %s'%self.__token, 'Accept': 'application/vnd.github.v3+json'}
			self.__readonly = False

			#if token is bad - we will be thrown from here with InvalidToken exception
			self.__name = None


	def name(self):
		try:
			if self.__name is None:
				self._rescanName()
		except CollectionSyncError:
			return 'INVALID'
		return self.__name

	def _rescanName(self):
		if(self.__readonly):return
		req=urllib2.Request(r'https://api.github.com/user',headers=self.__headers)
		code,rep=urlopen_nt(req)
		if (code != 200):
			if(code==403):raise InvalidToken('github auth failed')
			raise CollectionSyncError("unexpected server return level %d" % code)

		data=json.loads(rep.read())
		self.__name=data['login']

	def list(self):
		#this should produce list of snippets in the collection
		#the list should be a tuple of CollectionItem-s
		# note, that id is not a wid,
		requrl=r'https://api.github.com/gists'
		if(self.__readonly):requrl=r'https://api.github.com/users/%s/gists' % self.name()
		req=urllib2.Request(requrl,headers=self.__headers)
		code, rep = urlopen_nt(req)

		if(code!=200):
			if (code == 403): raise InvalidToken('github auth failed')
			raise CollectionSyncError("unexpected server return level %d" % code)

		log(str(rep.info()),0)
		data=json.loads(rep.read())
		gists=[x for x in data if '00_HPASTE_SNIPPET' in x['files']]
		if(len(gists)==0):
			return ()

		res=[]
		for gist in gists:
			files=gist['files']
			try:
				del files['00_HPASTE_SNIPPET']
			except KeyError:
				raise CollectionInconsistentError('impossible error! marker lost')
			#if(len(files)!=1):raise CollectionInconsistentError('each gist must have one marker and one file')
			desc=gist['description']
			nettype=''
			if(':' in desc):
				nettype,desc=desc.split(':',1)

			if(len(files)==1): # zero version implementation
				for filename in files:
					filedata=files[filename]
					rawurl=filedata['raw_url']
					retaccess = CollectionItem.AccessType.public if gist['public'] else CollectionItem.AccessType.private
					newitem=CollectionItem(self, filename, desc, '%s@%s'%(gist['id'], filename), retaccess, self.__readonly, metadata={'raw_url':rawurl, 'nettype':nettype, 'ver':(1, 0)})
					res.append(newitem)
			else:
				# first of all check version
				verfiles = [x for x in files if x.startswith("ver:")]
				if len(verfiles) != 1:
					log("skipping a broken collection item, id:%s" % gist['id'], 2)
					continue
				ver = map(lambda x: int(x), verfiles[0].split(':', 1)[1].split('.'))
				if ver[0] != currentVersion[0]:
					log("unsupported collection item version, id:%s" % gist['id'], 2)
					continue

				# now get the item
				itemfiles = [x for x in files if x.startswith("item:")]
				if len(itemfiles) != 1: raise CollectionInconsistentError('could not find unique item data in gist')
				filedata = files[itemfiles[0]]
				itemname = itemfiles[0].split(':', 1)[1]
				rawurl = filedata['raw_url']
				retaccess = CollectionItem.AccessType.public if gist['public'] else CollectionItem.AccessType.private
				newitem = CollectionItem(self, itemname, desc, '%s@%s'%(gist['id'], itemfiles[0]), retaccess, self.__readonly, metadata={'raw_url':rawurl, 'nettype':nettype, 'ver':ver})

				for typefilename in files:
					if ':' not in typefilename: continue
					filetype, filename = typefilename.split(':', 1)
					if filetype == 'item': continue  # Skip item file, it was already processed
					if filetype == 'icon':
						if not qtAvailable: continue  # If we were loaded from non-interactive session - just skip icons

						if ':' not in filename: continue
						icontype, iconname =  filename.split(':', 1)
						if icontype not in ['XPM-simple', 'PNG-base64']: continue  # supported icon format list

						filedata = files[typefilename]
						url = filedata['raw_url']
						if url in globalIconCacher:
							data = globalIconCacher[url]
						else:
							req = urllib2.Request(url, headers=self.__headers)
							code, rep = urlopen_nt(req)
							if (code != 200):
								if code == 403: raise InvalidToken('github auth failed')
								raise CollectionSyncError("unexpected server return level %d" % code)
							data = rep.read()

						if icontype == 'XPM-simple':
							newitem.metadata()['iconfullname'] = ':'.join((filetype, icontype, iconname))
							newitem.metadata()['icondata'] = data
							xpmlines = map(lambda x: x.replace('"', ''), re.findall('"[^"]*"', data))
							newitem.metadata()['iconpixmap'] = QPixmap(xpmlines)

						if icontype == 'PNG-base64':
							newitem.metadata()['iconfullname'] = ':'.join((filetype, icontype, iconname))
							newitem.metadata()['icondata'] = data
							pix = QPixmap()
							pix.loadFromData(base64.b64decode(data), "PNG")
							newitem.metadata()['iconpixmap'] = pix

				res.append(newitem)


		return tuple(res)

	def reinit(self):
		#this method should completely reread everything
		#rendering all existing items invalid
		raise NotImplementedError('Not yet implemented')

	def genWid(self,item):
		#this should generate hpaste compatible wid, that does not require collections to work
		assert isinstance(item,CollectionItem), 'item must be a collection item'

		raise NotImplementedError('Not yet implemented')

	def getContent(self,item):
		#this should bring the raw content of the collection item.
		assert isinstance(item, CollectionItem), 'item must be a collection item'

		req=urllib2.Request(item.metadata()['raw_url'],headers=self.__headers)
		code, rep = urlopen_nt(req)
		if(code!=200):
			if (code == 403): raise InvalidToken('github auth failed')
			raise CollectionSyncError("unexpected server return level %d" % code)

		data=rep.read()

		return data

	def readonly(self):
		return self.__readonly

	def updateItemIfNeeded(self, item):
		ver = tuple(item.metadata().get('ver', (1, 0)))
		if ver < currentVersion:  # TODO: put it into separate method
			log("upgrading collection item version to %s" % '.'.join(map(lambda x: str(x), currentVersion)))
			# if it's bigger but was still loadable - we don't change anything
			# For now we only know how to update 1.0 to 1.1
			if currentVersion == (1, 1):  # just in case i up the version and forget to change updater
				# we know exactly what's missing, so we just fix things, no checkings required
				id, filename = item.id().split('@', 1)
				data = {'files': {'ver:%s' % '.'.join(map(lambda x: str(x), currentVersion)): {'content': '==='}}}
				# ver 1.0 does not have ver: file, so we don't delete anything
				data['files'][filename] = {'filename': 'item:' + filename}

				req = urllib2.Request('https://api.github.com/gists/%s' % id, json.dumps(data), headers=self.__headers)
				req.get_method = lambda: 'PATCH'
				code, rep = urlopen_nt(req)
				if (code != 200):
					if (code == 403): raise InvalidToken('github auth failed')
					raise CollectionSyncError("unexpected server return level %d" % code)
				gist = json.loads(rep.read())

				itemfileanmes = [x for x in gist['files'].keys() if x.startswith("item:")]
				if len(itemfileanmes) != 1: raise CollectionInconsistentError('something went wrong during item version update: could not find unique item data in gist')
				newfilename = itemfileanmes[0]
				newname = newfilename.split(':', 1)[1]
				desc = gist['description']
				nettype = ''
				if (':' in desc):
					nettype, desc = desc.split(':', 1)
				item._desc = desc
				item._name = newname
				item._meta['raw_url'] = gist['files'][newfilename]['raw_url']
				item._meta['nettype'] = nettype
				item._id = '%s@%s' % (gist['id'], newfilename)
				item._access = CollectionItem.AccessType.public if gist['public'] else CollectionItem.AccessType.private
				item._readonly = False
			else:
				raise NotImplemented("version upgrade to %s is not implemented!" % '.'.join(map(lambda x: str(x), currentVersion)))

	def changeItem(self, item, newName=None, newDescription=None, newContent=None, newAccess=None, metadataChanges=None):
		assert isinstance(item, CollectionItem), 'item must be a collection item'
		#newName=str(newName)
		#newDescription=str(newDescription)
		#newContent=str(newContent)
		#just in case we have random unicode coming in
		#TODO: check that item belongs to this collection. just in case

		if item.readonly():raise CollectionReadonlyError()

		# Upgrade version if needed
		self.updateItemIfNeeded(item)

		if newAccess is not None and newAccess!=item.access():
			newitem=self.addItem(item.name() if newName is None else newName, item.description() if newDescription is None else newDescription, item.content() if newContent is None else newContent,newAccess, item.metadata())
			# all auxiliary files MUST BE COPIED before deleting original item
			# icon:
			id, filename = newitem.id().split('@', 1)
			if 'iconpixmap' in item.metadata() and 'iconfullname' in item.metadata() and 'icondata' in item.metadata():
				data = {'files':{item.metadata()['iconfullname']:{'content':item.metadata()['icondata']}}}
				req = urllib2.Request('https://api.github.com/gists/%s' % id, json.dumps(data), headers=self.__headers)
				req.get_method = lambda: 'PATCH'
				code, rep = urlopen_nt(req)
				if (code != 200):
					if (code == 403): raise InvalidToken('github auth failed')
					raise CollectionSyncError("unexpected server return level %d" % code)

			self.removeItem(copy.copy(item))  # remove the copy cuz item gets invalidated and we dont want that for original item
			item._name = newitem._name
			item._desc = newitem._desc
			item._meta['raw_url'] = newitem._meta['raw_url']
			item._meta['nettype'] = newitem._meta['nettype']
			item._id = newitem._id
			item._access=newitem._access
			item._readonly=newitem._readonly


			# if access is changed - we have to destroy this gist and create a new one with proper 'public' key
			# Butt Beware - we need to modify item's contents and return it WITHOUT reassigning the item itself
			# That's what we are doing here currently

			return

		if 'nettype' not in item.metadata():
			item.invalidate()
			raise CollectionItemInvalidError('required metadata was not found in the item')

		id, filename = item.id().split('@',1)
		data={}
		proceed=False
		if(newName is not None):
			if filename.startswith('item:'):  # new format. can cause trouble if someone actually called item starting with 'item:'
				data['filename'] = 'item:'+newName
			else:  # old format
				data['filename'] = newName
			proceed=True
		if(newContent is not None):
			data['content'] = newContent
			proceed = True
		if(data!={}):
			data={'files':{filename:data}}

		if(newDescription is not None):
			data['description']=':'.join((item.metadata()['nettype'], newDescription))
			proceed=True

		if proceed:
			req=urllib2.Request('https://api.github.com/gists/%s'%id,json.dumps(data), headers = self.__headers)
			req.get_method=lambda : 'PATCH'
			code, rep = urlopen_nt(req)
			if (code != 200):
				if (code == 403): raise InvalidToken('github auth failed')
				raise CollectionSyncError("unexpected server return level %d" % code)

			gist=json.loads(rep.read())
			newfilenames=gist['files'].keys()
			newfilenames.remove('00_HPASTE_SNIPPET')
			if(len(newfilenames) == 1):
				newfilename=newfilenames[0]
				newname = newfilename
			else:
				itemfileanmes = [x for x in newfilenames if x.startswith("item:")]
				if len(itemfileanmes) != 1: raise CollectionInconsistentError('something went wrong during item creation: could not find unique item data in gist')
				newfilename = itemfileanmes[0]
				newname = newfilename.split(':', 1)[1]
			desc = gist['description']
			nettype = ''
			if (':' in desc):
				nettype, desc = desc.split(':', 1)
			item._desc = desc
			item._name = newname
			item._meta['raw_url'] = gist['files'][newfilename]['raw_url']
			item._meta['nettype'] = nettype
			item._id = '%s@%s'%(gist['id'],newfilename)
			item._access = CollectionItem.AccessType.public if gist['public'] else CollectionItem.AccessType.private
			item._readonly = False

		# metadata changes processing
		if metadataChanges:
			metaspecialkeys = ['raw_url', 'nettype', 'icondata', 'iconfullname', 'iconpixmap', 'iconfullname', 'icondata']
			#if 'icondata' in metadataChanges and ('iconfullname' in item.metadata() or 'iconfullname' in metadataChanges):
				# Shall i implement this case? for when qt is not loaded
			if 'iconpixmap' in metadataChanges and qtAvailable:
				pix = metadataChanges['iconpixmap']
				if pix is None:  # removing pixmap
					if 'iconpixmap' in item.metadata():
						data = {'files':{item.metadata()['iconfullname']:None}}
						req = urllib2.Request('https://api.github.com/gists/%s' % item.id().split('@', 1)[0], json.dumps(data), headers=self.__headers)
						req.get_method = lambda: 'PATCH'
						code, rep = urlopen_nt(req)
						if (code != 200):
							if (code == 403): raise InvalidToken('github auth failed')
							raise CollectionSyncError("unexpected server return level %d" % code)
						rep.close()
						del item._meta['iconpixmap']
						del item._meta['iconfullname']
						del item._meta['icondata']
				else:
					barr = QByteArray()
					buff = QBuffer(barr)
					pix.save(buff, "PNG")
					imagedata = base64.b64encode(barr.data())
					buff.deleteLater()

					oldiconname = item.metadata().get('iconfullname', None)
					newiconname = 'icon:PNG-base64:autoicon'

					data = {'files':{}}
					if oldiconname is not None and oldiconname != newiconname:
						data['files'][oldiconname]=None

					data['files'][newiconname]={'content':imagedata}
					req = urllib2.Request('https://api.github.com/gists/%s' % item.id().split('@',1)[0], json.dumps(data), headers=self.__headers)
					req.get_method = lambda: 'PATCH'
					code, rep = urlopen_nt(req)
					if (code != 200):
						if (code == 403): raise InvalidToken('github auth failed')
						raise CollectionSyncError("unexpected server return level %d" % code)
					replydict = json.loads(rep.read())

					if newiconname not in replydict['files']:
						raise CollectionSyncError("icon file was not uploaded properly")

					globalIconCacher[replydict['files'][newiconname]['raw_url']] = imagedata
					item._meta['iconfullname'] = newiconname
					item._meta['icondata'] = imagedata
					item._meta['iconpixmap'] = pix

			for metakey in metadataChanges.keys():
				# All special cases are taken care of, so now just blind copy all remaining changes
				if metakey in metaspecialkeys: continue
				item._meta[metakey] = metadataChanges[metakey]

	def addItem(self,desiredName,description,content, access=CollectionItem.AccessType.private, metadata=None):
		assert isinstance(desiredName,str) or isinstance(desiredName,unicode), 'name should be a string'
		assert isinstance(content,str) or isinstance(content,unicode), 'conetnt shoud be a string'
		assert access==0 or access==1, 'wrong access type'  #TODO there's no other type enforcement for this const for now

		if(self.__readonly):raise CollectionReadonlyError('collection is opened as read-only!')

		if ('nettype' not in metadata):
			raise CollectionItemInvalidError('required metadata must be present in metadata')

		description=":".join((metadata['nettype'],description))
		postdata = {'public': access==CollectionItem.AccessType.public, 'description': description}
		postdata['files'] = {'00_HPASTE_SNIPPET': {'content': 'snippets marker'}}
		postdata['files']['item:'+desiredName] = {'content':content}
		postdata['files']['ver:%s' % '.'.join(map(lambda x:str(x), currentVersion))] = {'content':'==='}

		req = urllib2.Request(r'https://api.github.com/gists', json.dumps(postdata), headers=self.__headers)
		code, rep = urlopen_nt(req)
		if (code != 201):
			if (code == 403): raise InvalidToken('github auth failed')
			raise CollectionSyncError("unexpected server return level %d" % code)

		gist=json.loads(rep.read())
		newfilenames = gist['files'].keys()
		newfilenames.remove('00_HPASTE_SNIPPET')
		if (len(newfilenames) == 1):
			newfilename = newfilenames[0]
			newname = newfilename
		else:
			itemfileanmes = [x for x in newfilenames if x.startswith("item:")]
			if len(itemfileanmes) != 1: raise CollectionInconsistentError('something went wrong during item creation: could not find unique item data in gist')
			newfilename = itemfileanmes[0]
			newname = newfilename.split(':',1)[1]

		if metadata is None: metadata={}
		metadata['raw_url'] = gist['files'][newfilename]['raw_url']
		desc = gist['description']
		nettype = ''
		if (':' in desc):
			nettype, desc = desc.split(':', 1)
		metadata['nettype'] = nettype
		retaccess = CollectionItem.AccessType.public if gist['public'] else CollectionItem.AccessType.private
		newitem = CollectionItem(self, newname, desc, '%s@%s' % (gist['id'], newfilename), retaccess, False, metadata)
		return newitem

	def removeItem(self,item):
		assert isinstance(item, CollectionItem), 'item must be a collection item'
		if (item.readonly()): raise CollectionReadonlyError()

		id, name = item.id().split('@', 1)

		req = urllib2.Request(r'https://api.github.com/gists/%s'%id, headers=self.__headers)
		req.get_method=lambda : 'DELETE'
		code, rep = urlopen_nt(req)
		if (code != 204):
			if (code == 403): raise InvalidToken('github auth failed')
			raise CollectionSyncError("unexpected server return level %d" % code)

		item.invalidate()

if(__name__=='__main__'):
	from os import path
	#testing

	testToken = ''
	with open(path.join(path.dirname(path.dirname(path.dirname(path.dirname(__file__)))), 'githubtoken.tok'),'r') as f:
		testToken=f.read()
		testToken=testToken.replace('\n','')
	print(testToken)
	col=GithubCollection(testToken)

	res=col.list()
	for obj in res:
		log(obj.id())
	#log(res[0].content())

	#log(col.changeItem(res[0]))

