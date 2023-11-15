from os import listdir, remove, rename
from os.path import join
from time import localtime, strftime, time

from enigma import eActionMap, eDVBDB, eEPGCache, ePoint, eRCInput, eServiceCenter, eServiceReference, eServiceReferenceDVB, eTimer, getPrevAsciiCode, iPlayableService, iServiceInformation, loadPNG

from RecordTimer import AFTEREVENT, RecordTimerEntry, TIMERTYPE
from ServiceReference import ServiceReference, hdmiInServiceRef, serviceRefAppendPath, service_types_radio_ref, service_types_tv_ref
from skin import getSkinFactor
from Components.ActionMap import ActionMap, HelpableActionMap, NumberActionMap
from Components.Button import Button
from Components.ChoiceList import ChoiceEntryComponent, ChoiceList
from Components.config import ConfigSubsection, ConfigText, ConfigYesNo, config, configfile
from Components.Input import Input
from Components.MenuList import MenuList
from Components.NimManager import nimmanager
from Components.ParentalControl import parentalControl
from Components.PluginComponent import plugins
from Components.ServiceEventTracker import ServiceEventTracker, InfoBarBase
from Components.ServiceList import ServiceList, refreshServiceList
from Components.SystemInfo import BoxInfo, getBoxDisplayName
from Components.UsageConfig import preferredTimerPath
from Components.Renderer.Picon import getPiconName
from Components.Sources.Event import Event
from Components.Sources.List import List
from Components.Sources.RdsDecoder import RdsDecoder
from Components.Sources.ServiceEvent import ServiceEvent
from Components.Sources.StaticText import StaticText
from Plugins.Plugin import PluginDescriptor
try:
	from Plugins.SystemPlugins.PiPServiceRelation.plugin import getRelationDict
	plugin_PiPServiceRelation_installed = True
except ImportError:
	plugin_PiPServiceRelation_installed = False
from Screens.ButtonSetup import ButtonSetupActionMap, InfoBarButtonSetup, getButtonSetupFunctions
from Screens.ChoiceBox import ChoiceBox
from Screens.EpgSelection import EPGSelection
from Screens.EventView import EventViewEPGSelect
from Screens.HelpMenu import HelpableScreen
import Screens.InfoBar
from Screens.InputBox import PinInput
from Screens.MessageBox import MessageBox
from Screens.PictureInPicture import PictureInPicture
from Screens.RdsDisplay import RassInteractive
from Screens.Screen import Screen
from Screens.Setup import Setup
from Screens.TimerEdit import TimerSanityConflict
from Screens.TimerEntry import InstantRecordTimerEntry, TimerEntry
from Screens.VirtualKeyBoard import VirtualKeyBoard
from Tools.BoundFunction import boundFunction
from Tools.Directories import fileWriteLine
from Tools.Notifications import AddPopup, RemovePopup
from Tools.NumericalTextInput import NumericalTextInput

MODE_TV = 0
MODE_RADIO = 1

HISTORY_SIZE = 20

FLAG_SERVICE_NEW_FOUND = 64
FLAG_IS_DEDICATED_3D = 128
FLAG_HIDE_VBI = 512
FLAG_CENTER_DVB_SUBS = 2048  # Defined in lib/dvb/idvb.h as dxNewFound = 64 and dxIsDedicated3D = 128.

# Values for csel.bouquet_mark_edit:
OFF = 0
EDIT_OFF = 0
EDIT_BOUQUET = 1
EDIT_ALTERNATIVES = 2
EDIT_FAVORITE = 3
EDIT_MOVE = 4
EDIT_PIP = 5

service_types_tv = service_types_tv_ref.toString()
service_types_radio = service_types_radio_ref.toString()

multibouquet_tv_ref = eServiceReference(service_types_tv_ref)
multibouquet_tv_ref.setPath("FROM BOUQUET \"bouquets.tv\" ORDER BY bouquet")

singlebouquet_tv_ref = serviceRefAppendPath(service_types_tv_ref, " FROM BOUQUET \"userbouquet.favourites.tv\" ORDER BY bouquet")

multibouquet_radio_ref = eServiceReference(service_types_radio_ref)
multibouquet_radio_ref.setPath("FROM BOUQUET \"bouquets.radio\" ORDER BY bouquet")

singlebouquet_radio_ref = serviceRefAppendPath(service_types_radio_ref, " FROM BOUQUET \"userbouquet.favourites.radio\" ORDER BY bouquet")


class SilentBouquetSelector:
	def __init__(self, bouquets, enableWrapAround=False, current=0):
		self.bouquets = [b[1] for b in bouquets]
		self.pos = current
		self.count = len(bouquets)
		self.enableWrapAround = enableWrapAround

	def up(self):
		if self.pos > 0 or self.enableWrapAround:
			self.pos = (self.pos - 1) % self.count

	def down(self):
		if self.pos < (self.count - 1) or self.enableWrapAround:
			self.pos = (self.pos + 1) % self.count

	def getCurrent(self):
		return self.bouquets[self.pos]


def removed_userbouquets_available():
	for file in listdir("/etc/enigma2/"):
		if file.startswith("userbouquet") and file.endswith(".del"):
			return True
	return False


class SelectionEventInfo:
	def __init__(self):
		self["Service"] = self["ServiceEvent"] = ServiceEvent()
		self["Event"] = Event()
		self.servicelist.connectSelChanged(self.__selectionChanged)
		self.timer = eTimer()
		self.timer.callback.append(self.updateEventInfo)
		self.onShown.append(self.__selectionChanged)

	def __selectionChanged(self):
		if self.execing:
			self.timer.start(100, True)

	def updateEventInfo(self):
		cur = self.getCurrentSelection()
		service = self["Service"]
		service.newService(cur)
		self["Event"].newEvent(service.event)


def parseCurentEvent(list, isZapTimer=False):
	if len(list) >= 0:
		list = list[0]
		begin = list[2] - (getattr(config.recording, "zap_margin_before" if isZapTimer else "margin_before").value * 60)
		end = list[2] + list[3] + (getattr(config.recording, "zap_margin_after" if isZapTimer else "margin_after").value * 60)
		name = list[1]
		description = list[5]
		eit = list[0]
		return begin, end, name, description, eit
	return False


def parseNextEvent(list, isZapTimer=False):
	if len(list) > 0:
		list = list[1]
		begin = list[2] - (getattr(config.recording, "zap_margin_before" if isZapTimer else "margin_before").value * 60)
		end = list[2] + list[3] + (getattr(config.recording, "zap_margin_after" if isZapTimer else "margin_after").value * 60)
		name = list[1]
		description = list[5]
		eit = list[0]
		return begin, end, name, description, eit
	return False


class ChannelSelectionEPG(InfoBarButtonSetup):
	def __init__(self):
		self.ChoiceBoxDialog = None
		self.RemoveTimerDialog = None
		self.hotkeys = [
			("Info (EPG)", "info", "Infobar/openEventView"),
			("Info (EPG)" + " " + _("long"), "info_long", "Infobar/showEventInfoPlugins"),
			("Epg/Guide", "epg", "Infobar/EPGPressed/1"),
			("Epg/Guide" + " " + _("long"), "epg_long", "Infobar/showEventInfoPlugins")
		]
		self["ChannelSelectEPGActions"] = ButtonSetupActionMap(["ChannelSelectEPGActions"], dict((x[1], self.ButtonSetupGlobal) for x in self.hotkeys))
		self.currentSavedPath = []
		self.onExecBegin.append(self.clearLongKeyPressed)
		self["ChannelSelectEPGActions"] = ActionMap(["ChannelSelectInfoActions", "ChannelSelectEPGActions"], {
			"showEPGList": self.showEPGList,
			"showEventInfo": self.showEventInfo
		})
		self["recordingactions"] = HelpableActionMap(self, ["InfobarInstantRecord"], {
			"ShortRecord": (self.RecordTimerQuestion, _("Add a RecordTimer")),
			"LongRecord": (self.doZapTimer, _("Add a ZapTimer for next event"))
		}, prio=-1)
		self["dialogactions"] = ActionMap(["SetupActions"], {
			"cancel": self.closeChoiceBoxDialog,
		})
		self["dialogactions"].execEnd()

	def getKeyFunctions(self, key):
		selection = eval("config.misc.ButtonSetup." + key + ".value.split(\",\")")
		# selection = getattr(config.misc.ButtonSetup", key).value.split(",")  # TODO: Can "key" be invalid?
		selected = []
		for x in selection:
			function = list(function for function in getButtonSetupFunctions() if function[1] == x and function[2] == "EPG")
			if function:
				selected.append(function[0])
		return selected

	def RecordTimerQuestion(self):
		serviceref = ServiceReference(self.getCurrentSelection())
		refstr = ":".join(serviceref.ref.toString().split(":")[:11])
		self.epgcache = eEPGCache.getInstance()
		test = ["ITBDSECX", (refstr, 1, -1, 12 * 60)]  # Search next 12 hours.
		self.list = [] if self.epgcache is None else self.epgcache.lookupEvent(test)
		if len(self.list) < 1:
			return
		eventid = self.list[0][0]
		if len(self.list) == 1:
			eventidnext = None
		else:
			eventidnext = self.list[1][0]
		eventname = str(self.list[0][1])
		if eventid is None:
			return
		menu1 = _("Record now")
		menu2 = _("Record next")
		for timer in self.session.nav.RecordTimer.timer_list:
			if timer.eit == eventid and ":".join(timer.service_ref.ref.toString().split(":")[:11]) == refstr:
				menu1 = _("Stop recording now")
			elif eventidnext is not None:
				if timer.eit == eventidnext and ":".join(timer.service_ref.ref.toString().split(":")[:11]) == refstr:
					menu2 = _("Change next timer")
		if eventidnext is not None:
			menu = [
				(menu1, "CALLFUNC", self.ChoiceBoxCB, self.doRecordCurrentTimer),
				(menu2, "CALLFUNC", self.ChoiceBoxCB, self.doRecordNextTimer)
			]
			if menu2 == _("Record next"):
				menu.append((_("Zap next"), "CALLFUNC", self.ChoiceBoxCB, self.doZapTimer))
				if not TIMERTYPE.ALWAYS_ZAP:
					menu.append((_("Zap+Record next"), "CALLFUNC", self.ChoiceBoxCB, self.doZapRecordTimer))
		else:
			menu = [(menu1, "CALLFUNC", self.ChoiceBoxCB, self.doRecordCurrentTimer)]
		self.ChoiceBoxDialog = self.session.instantiateDialog(ChoiceBox, list=menu, keys=["red", "green", "yellow", "blue"], skin_name="RecordTimerQuestion")
		self.setChoiceBoxDialogPosition()
		self.showChoiceBoxDialog()

	def ChoiceBoxCB(self, choice):
		self.closeChoiceBoxDialog()
		if choice:
			try:
				choice()
			except Exception:
				choice

	def RemoveTimerDialogCB(self, choice):
		self.closeChoiceBoxDialog()
		if choice:
			choice(self)

	def showChoiceBoxDialog(self):
		self["actions"].setEnabled(False)
		self["recordingactions"].setEnabled(False)
		self["ChannelSelectEPGActions"].setEnabled(False)
		self["ChannelSelectBaseActions"].setEnabled(False)
		self["dialogactions"].execBegin()
		self.ChoiceBoxDialog["actions"].execBegin()
		self.ChoiceBoxDialog.show()

	def closeChoiceBoxDialog(self, choice=None):
		self["dialogactions"].execEnd()
		if self.ChoiceBoxDialog:
			self.ChoiceBoxDialog["actions"].execEnd()
			self.session.deleteDialog(self.ChoiceBoxDialog)
		self["actions"].setEnabled(True)
		self["recordingactions"].setEnabled(True)
		self["ChannelSelectEPGActions"].setEnabled(True)
		self["ChannelSelectBaseActions"].setEnabled(True)

	def doRecordCurrentTimer(self):
		self.doInstantTimer(0, TIMERTYPE.ALWAYS_ZAP, parseCurentEvent)

	def doRecordNextTimer(self):
		self.doInstantTimer(0, TIMERTYPE.ALWAYS_ZAP, parseNextEvent, True)

	def doZapTimer(self):
		self.doInstantTimer(1, 0, parseNextEvent, True)

	def doZapRecordTimer(self):
		self.doInstantTimer(0, 1, parseNextEvent, True)

	def editTimer(self, timer):
		self.session.open(TimerEntry, timer)

	def doInstantTimer(self, zap, zaprecord, parseEvent, next=False):
		serviceref = ServiceReference(self.getCurrentSelection())
		refstr = ":".join(serviceref.ref.toString().split(":")[:11])
		self.epgcache = eEPGCache.getInstance()
		test = ["ITBDSECX", (refstr, 1, -1, 12 * 60)]  # Search next 12 hours.
		self.list = [] if self.epgcache is None else self.epgcache.lookupEvent(test)
		if self.list is None:
			return
		if not next:
			eventid = self.list[0][0]
			eventname = str(self.list[0][1])
		else:
			if len(self.list) < 2:
				return
			eventid = self.list[1][0]
			eventname = str(self.list[1][1])
		if eventid is None:
			return
		for timer in self.session.nav.RecordTimer.timer_list:
			if timer.eit == eventid and ":".join(timer.service_ref.ref.toString().split(":")[:11]) == refstr:
				rt_func = lambda ret: self.removeTimer(timer)
				if not next:
					menu = [(_("Delete Timer"), "CALLFUNC", rt_func), (_("No"), "CALLFUNC", self.closeChoiceBoxDialog)]
					title = _("Do you really want to remove the timer for %s?") % eventname
				else:
					cb_func2 = lambda ret: self.editTimer(timer)
					menu = [
						(_("Delete Timer"), "CALLFUNC", self.RemoveTimerDialogCB, rt_func),
						(_("Edit Timer"), "CALLFUNC", self.RemoveTimerDialogCB, cb_func2)
					]
					title = _("Select action for timer %s:") % eventname
				self.ChoiceBoxDialog = self.session.instantiateDialog(ChoiceBox, title=title, list=menu, keys=["red", "green"], skin_name="RecordTimerQuestion")
				self.setChoiceBoxDialogPosition()
				self.showChoiceBoxDialog()
				break
		else:
			newEntry = RecordTimerEntry(serviceref, checkOldTimers=True, dirname=preferredTimerPath(), *parseEvent(self.list, isZapTimer=zap))
			if not newEntry:
				return
			self.InstantRecordDialog = self.session.instantiateDialog(InstantRecordTimerEntry, newEntry, zap, zaprecord)
			retval = [True, self.InstantRecordDialog.retval()]
			self.session.deleteDialogWithCallback(self.finishedAdd, self.InstantRecordDialog, retval)

	def setChoiceBoxDialogPosition(self):
		indx = self.servicelist.getCurrentIndex()
		ipp = self.servicelist.instance.size().height() / self.servicelist.ItemHeight
		while indx + 1 > ipp:
			indx -= ipp
		sf = getSkinFactor()
		selx = min(self.servicelist.instance.size().width() + self.servicelist.instance.position().x(), 1280 * sf)
		sely = min(self.servicelist.instance.position().y() + (self.servicelist.ItemHeight * indx), 720 * sf)
		posx = max(self.instance.position().x() + selx - self.ChoiceBoxDialog.instance.size().width() - 20 * sf, 0)
		posy = self.instance.position().y() + sely
		posy += self.servicelist.ItemHeight - 2 * sf
		if posy + self.ChoiceBoxDialog.instance.size().height() > 720 * sf:
			posy -= self.servicelist.ItemHeight - 4 * sf + self.ChoiceBoxDialog.instance.size().height()
		self.ChoiceBoxDialog.instance.move(ePoint(int(posx), int(posy)))

	def finishedAdd(self, answer):
		# print("[ChannelSelection] Finished add.")
		if isinstance(answer, bool) and answer:  # Special case for close recursive.
			self.close(True)
			return
		if answer[0]:
			entry = answer[1]
			simulTimerList = self.session.nav.RecordTimer.record(entry)
			if simulTimerList is not None:
				for x in simulTimerList:
					if x.setAutoincreaseEnd(entry):
						self.session.nav.RecordTimer.timeChanged(x)
				simulTimerList = self.session.nav.RecordTimer.record(entry)
				if simulTimerList is not None:
					if not entry.repeated and not config.recording.margin_before.value and not config.recording.margin_after.value and len(simulTimerList) > 1:
						change_time = False
						conflict_begin = simulTimerList[1].begin
						conflict_end = simulTimerList[1].end
						if conflict_begin == entry.end:
							entry.end -= 30
							change_time = True
						elif entry.begin == conflict_end:
							entry.begin += 30
							change_time = True
						if change_time:
							simulTimerList = self.session.nav.RecordTimer.record(entry)
					if simulTimerList is not None:
						self.session.openWithCallback(self.finishSanityCorrection, TimerSanityConflict, simulTimerList)

	def finishSanityCorrection(self, answer):
		self.finishedAdd(answer)

	def removeTimer(self, timer):
		timer.afterEvent = AFTEREVENT.NONE
		self.session.nav.RecordTimer.removeEntry(timer)
		self.closeChoiceBoxDialog()

	def showEPGList(self):
		ref = self.getCurrentSelection()
		if ref:
			self.savedService = ref
			self.session.openWithCallback(self.SingleServiceEPGClosed, EPGSelection, ref, serviceChangeCB=self.changeServiceCB, EPGtype="single")

	def showEventInfo(self):
		if config.usage.servicelist_infokey.value == "epg":
			self.showEPGList()
			return
		ref = self.getCurrentSelection()
		if ref:
			epglist = []
			epg = eEPGCache.getInstance()
			ptr = ref and ref.valid() and epg.lookupEventTime(ref, -1)
			if ptr:
				epglist.append(ptr)
				ptr = epg.lookupEventTime(ref, ptr.getBeginTime(), +1)
				if ptr:
					epglist.append(ptr)
				if epglist:
					self.epglist = epglist
					self.session.open(EventViewEPGSelect, epglist[0], ServiceReference(ref), self.eventViewCallback, similarEPGCB=self.eventViewSimilarCallback)

	def eventViewCallback(self, setEvent, setService, val):
		epglist = self.epglist
		if len(epglist) > 1:
			tmp = epglist[0]
			epglist[0] = epglist[1]
			epglist[1] = tmp
			setEvent(epglist[0])

	def eventViewSimilarCallback(self, eventid, refstr):
		self.session.open(EPGSelection, refstr, None, eventid)

	def SingleServiceEPGClosed(self, ret=False):
		if ret:
			service = self.getCurrentSelection()
			if service is not None:
				self.saveChannel(service)
				self.addToHistory(service)
				self.close()
		else:
			self.setCurrentSelection(self.savedService)

	def changeServiceCB(self, direction, epg):
		beg = self.getCurrentSelection()
		while True:
			if direction > 0:
				self.moveDown()
			else:
				self.moveUp()
			cur = self.getCurrentSelection()
			if cur == beg or not (cur.flags & eServiceReference.isMarker):
				break
		epg.setService(ServiceReference(self.getCurrentSelection()))

	def zapToService(self, service, preview=False, zapback=False):
		if self.startServiceRef is None:
			self.startServiceRef = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if service is not None:
			if self.servicelist.getRoot() != self.epg_bouquet:
				self.servicelist.clearPath()
				if self.servicelist.bouquet_root != self.epg_bouquet:
					self.servicelist.enterPath(self.servicelist.bouquet_root)
				self.servicelist.enterPath(self.epg_bouquet)
			self.servicelist.setCurrent(service)
		if not zapback or preview:
			self.zap(enable_pipzap=True)
		if (self.dopipzap or zapback) and not preview:
			self.zapBack()
		if not preview:
			self.startServiceRef = None
			self.startRoot = None


class ChannelSelectionEdit:
	def __init__(self):
		self.entry_marked = False
		self.bouquet_mark_edit = EDIT_OFF
		self.mutableList = None
		self.__marked = []
		self.saved_root = None
		self.current_ref = None
		self.editMode = False
		self.confirmRemove = True

		class ChannelSelectionEditActionMap(ActionMap):
			def __init__(self, csel, contexts=None, actions=None, prio=0):
				if not contexts:
					contexts = []
				if not actions:
					actions = {}
				ActionMap.__init__(self, contexts, actions, prio)
				self.csel = csel

			def action(self, contexts, action):
				if action == "cancel":
					self.csel.handleEditCancel()
					return 0  # Fall-through.
				elif action == "ok":
					return 0  # Fall-through.
				else:
					return ActionMap.action(self, contexts, action)

		self["ChannelSelectEditActions"] = ChannelSelectionEditActionMap(self, ["ChannelSelectEditActions", "OkCancelActions"], {
			"contextMenu": self.doContext,
		})

	def getMutableList(self, root=eServiceReference()):
		if self.mutableList is not None:
			return self.mutableList
		serviceHandler = eServiceCenter.getInstance()
		if not root.valid():
			root = self.getRoot()
		list = root and serviceHandler.list(root)
		if list is not None:
			return list.startEdit()
		return None

	def buildBouquetID(self, str):
		tmp = str.lower()
		name = ""
		for c in tmp:
			if ("a" <= c <= "z") or ("0" <= c <= "9"):
				name += c
			else:
				name += "_"
		return name

	def renameEntry(self):
		self.editMode = True
		cur = self.getCurrentSelection()
		if cur and cur.valid():
			name = eServiceCenter.getInstance().info(cur).getName(cur) or ServiceReference(cur).getServiceName() or ""
			name = name.replace("\xc2\x86", "").replace("\xc2\x87", "")
			if name:
				self.session.openWithCallback(self.renameEntryCallback, VirtualKeyBoard, title=_("Please enter new name:"), text=name)
		else:
			return 0

	def renameEntryCallback(self, name):
		if name:
			mutableList = self.getMutableList()
			if mutableList:
				current = self.servicelist.getCurrent()
				current.setName(name)
				index = self.servicelist.getCurrentIndex()
				mutableList.removeService(current, False)
				mutableList.addService(current)
				mutableList.moveService(current, index)
				mutableList.flushChanges()
				self.servicelist.addService(current, True)
				self.servicelist.removeCurrent()
				if not self.servicelist.atEnd():
					self.servicelist.moveUp()

	def addHDMIIn(self, name):
		current = self.servicelist.getCurrent()
		mutableList = self.getMutableList()
		ref = hdmiInServiceRef()
		ref.setName(name)
		if mutableList and current and current.valid():
			if not mutableList.addService(ref, current):
				self.servicelist.addService(ref, True)
				mutableList.flushChanges()

	def addMarker(self, name):
		current = self.servicelist.getCurrent()
		mutableList = self.getMutableList()
		cnt = 0
		while mutableList:
			ref = eServiceReference(eServiceReference.idDVB, eServiceReference.isMarker, cnt)
			ref.setName(name)
			if current and current.valid():
				if not mutableList.addService(ref, current):
					self.servicelist.addService(ref, True)
					mutableList.flushChanges()
					break
			elif not mutableList.addService(ref):
				self.servicelist.addService(ref, True)
				mutableList.flushChanges()
				break
			cnt += 1

	def addAlternativeServices(self):
		cur_service = ServiceReference(self.getCurrentSelection())
		root = self.getRoot()
		cur_root = root and ServiceReference(root)
		mutableBouquet = cur_root.list().startEdit()
		if mutableBouquet:
			name = cur_service.getServiceName()
			flags = eServiceReference.isGroup | eServiceReference.canDescent | eServiceReference.mustDescent
			if self.mode == MODE_TV:
				ref = eServiceReference(eServiceReference.idDVB, flags, eServiceReferenceDVB.dTv)
				ref.setPath("FROM BOUQUET \"alternatives.%s.tv\" ORDER BY bouquet" % self.buildBouquetID(name))
			else:
				ref = eServiceReference(eServiceReference.idDVB, flags, eServiceReferenceDVB.dRadio)
				ref.setPath("FROM BOUQUET \"alternatives.%s.radio\" ORDER BY bouquet" % self.buildBouquetID(name))
			new_ref = ServiceReference(ref)
			if not mutableBouquet.addService(new_ref.ref, cur_service.ref):
				mutableBouquet.removeService(cur_service.ref)
				mutableBouquet.flushChanges()
				eDVBDB.getInstance().reloadBouquets()
				mutableAlternatives = new_ref.list().startEdit()
				if mutableAlternatives:
					mutableAlternatives.setListName(name)
					if mutableAlternatives.addService(cur_service.ref):
						print("[ChannelSelection] Add '%s' to new alternatives failed!" % cur_service.ref.toString())
					mutableAlternatives.flushChanges()
					self.servicelist.addService(new_ref.ref, True)
					self.servicelist.removeCurrent()
					if not self.atEnd():
						self.servicelist.moveUp()
					if cur_service.ref.toString() == self.lastservice.value:
						self.saveChannel(new_ref.ref)
					if self.startServiceRef and cur_service.ref == self.startServiceRef:
						self.startServiceRef = new_ref.ref
				else:
					print("[ChannelSelection] Get mutable list for new created alternatives failed!")
			else:
				print("[ChannelSelection] Add '%s' to '%s' failed!" % (str, cur_root.getServiceName()))
		else:
			print("[ChannelSelection] The bouquet list is not editable.")

	def addBouquet(self, bName, services):
		serviceHandler = eServiceCenter.getInstance()
		mutableBouquetList = serviceHandler.list(self.bouquet_root).startEdit()
		if mutableBouquetList:
			if self.mode == MODE_TV:
				bName = "%s %s" % (bName, _("(TV)"))
				new_bouquet_ref = eServiceReference(service_types_tv_ref)
				new_bouquet_ref.setPath("FROM BOUQUET \"userbouquet.%s.tv\" ORDER BY bouquet" % self.buildBouquetID(bName))
			else:
				bName = "%s %s" % (bName, _("(Radio)"))
				new_bouquet_ref = eServiceReference(service_types_radio_ref)
				new_bouquet_ref.setPath("FROM BOUQUET \"userbouquet.%s.radio\" ORDER BY bouquet" % self.buildBouquetID(bName))
			if not mutableBouquetList.addService(new_bouquet_ref):
				mutableBouquetList.flushChanges()
				eDVBDB.getInstance().reloadBouquets()
				mutableBouquet = serviceHandler.list(new_bouquet_ref).startEdit()
				if mutableBouquet:
					mutableBouquet.setListName(bName)
					if services is not None:
						for service in services:
							if mutableBouquet.addService(service):
								print("[ChannelSelection] Add '%s' to new bouquet failed!" % service.toString())
					mutableBouquet.flushChanges()
				else:
					print("[ChannelSelection] Get mutable list for new created bouquet failed!")
				# Do some voodoo to check if current_root is equal to bouquet_root.
				cur_root = self.getRoot()
				str1 = cur_root and cur_root.getPath()
				pos1 = str1.find("FROM BOUQUET") if str1 else -1
				pos2 = self.bouquet_root.getPath().find("FROM BOUQUET")
				if pos1 != -1 and pos2 != -1 and str1[pos1:] == self.bouquet_root.getPath()[pos2:]:
					self.servicelist.addService(new_bouquet_ref)
					self.servicelist.resetRoot()
			else:
				print("[ChannelSelection] Add '%s' to bouquets failed!" % new_bouquet_ref.toString())
		else:
			print("[ChannelSelection] The bouquet list is not editable.")

	def copyCurrentToBouquetList(self):
		provider = ServiceReference(self.getCurrentSelection())
		providerName = provider.getServiceName()
		serviceHandler = eServiceCenter.getInstance()
		services = serviceHandler.list(provider.ref)
		self.addBouquet(providerName, services and services.getContent("R", True))

	def copyCurrentToStreamRelay(self):
		provider = ServiceReference(self.getCurrentSelection())
		serviceHandler = eServiceCenter.getInstance()
		services = serviceHandler.list(provider.ref)
		Screens.InfoBar.InfoBar.instance.ToggleStreamrelay(services and services.getContent("R", True))

	def removeAlternativeServices(self):
		cur_service = ServiceReference(self.getCurrentSelection())
		end = self.atEnd()
		root = self.getRoot()
		cur_root = root and ServiceReference(root)
		list = cur_service.list()
		first_in_alternative = list and list.getNext()
		if first_in_alternative:
			edit_root = cur_root and cur_root.list().startEdit()
			if edit_root:
				if not edit_root.addService(first_in_alternative, cur_service.ref):
					self.servicelist.addService(first_in_alternative, True)
					if cur_service.ref.toString() == self.lastservice.value:
						self.saveChannel(first_in_alternative)
					if self.startServiceRef and cur_service.ref == self.startServiceRef:
						self.startServiceRef = first_in_alternative
				else:
					print("[ChannelSelection] Couldn't add first alternative service to current root!")
			else:
				print("[ChannelSelection] Couldn't edit current root!")
		else:
			print("[ChannelSelection] Remove empty alternative list!")
		self.removeBouquet()
		if not end:
			self.servicelist.moveUp()

	def removeBouquet(self):
		# refstr = self.getCurrentSelection().toString()  # DEBUG NOTE: This doesn't appear to be used.
		# pos = refstr.find("FROM BOUQUET \"")  # DEBUG NOTE: This doesn't appear to be used.
		filename = None
		self.removeCurrentService(bouquet=True)

	def removeSatelliteService(self):
		current = self.getCurrentSelection()
		eDVBDB.getInstance().removeService(current)
		refreshServiceList()
		if not self.atEnd():
			self.servicelist.moveUp()

	def removeSatelliteServices(self):
		current = self.getCurrentSelection()
		unsigned_orbpos = current.getUnsignedData(4) >> 16
		if unsigned_orbpos == 0xFFFF:
			messageText = _("Are you sure to remove all cable services?")
		elif unsigned_orbpos == 0xEEEE:
			messageText = _("Are you sure to remove all terrestrial services?")
		else:
			if unsigned_orbpos > 1800:
				unsigned_orbpos = 3600 - unsigned_orbpos
				direction = _("W")
			else:
				direction = _("E")
			messageText = _("Are you sure to remove all %d.%d%s%s services?") % (unsigned_orbpos / 10, unsigned_orbpos % 10, "\u00B0", direction)
		self.session.openWithCallback(self.removeSatelliteServicesCallback, MessageBox, messageText)

	def removeSatelliteServicesCallback(self, answer):
		if answer:
			currentIndex = self.servicelist.getCurrentIndex()
			current = self.getCurrentSelection()
			unsigned_orbpos = current.getUnsignedData(4) >> 16
			if unsigned_orbpos == 0xFFFF:
				eDVBDB.getInstance().removeServices(int("0xFFFF0000", 16) - 0x100000000)
			elif unsigned_orbpos == 0xEEEE:
				eDVBDB.getInstance().removeServices(int("0xEEEE0000", 16) - 0x100000000)
			else:
				curpath = current.getPath()
				idx = curpath.find("satellitePosition == ")
				if idx != -1:
					tmp = curpath[idx + 21:]
					idx = tmp.find(")")
					if idx != -1:
						satpos = int(tmp[:idx])
						eDVBDB.getInstance().removeServices(-1, -1, -1, satpos)
			refreshServiceList()
			if hasattr(self, "showSatellites"):
				self.showSatellites()
				self.servicelist.moveToIndex(currentIndex)
				if currentIndex != self.servicelist.getCurrentIndex():
					self.servicelist.instance.moveSelection(self.servicelist.instance.moveEnd)

	def startMarkedEdit(self, functionType):  # Multiple marked entry stuff (edit mode, later multiEPG selection).
		self.savedPath = self.servicePath[:]
		if functionType == EDIT_ALTERNATIVES:
			self.current_ref = self.getCurrentSelection()
			self.enterPath(self.current_ref)
		self.mutableList = self.getMutableList()
		# Add all services from the current list to internal marked set in listboxServiceContent.
		self.clearMarks()  # This clears the internal marked set in the listboxServiceContent.
		if functionType == EDIT_ALTERNATIVES:
			self.bouquet_mark_edit = EDIT_ALTERNATIVES
		else:
			self.bouquet_mark_edit = EDIT_BOUQUET
			functionType = EDIT_BOUQUET if config.usage.multibouquet.value else EDIT_FAVORITE
		self.function = functionType
		self.buildTitle()
		self.__marked = self.servicelist.getRootServices()
		for x in self.__marked:
			self.servicelist.addMarked(eServiceReference(x))
		self["Service"].editmode = True

	def endMarkedEdit(self, abort):
		if not abort and self.mutableList is not None:
			new_marked = set(self.servicelist.getMarked())
			old_marked = set(self.__marked)
			removed = old_marked - new_marked
			added = new_marked - old_marked
			changed = False
			for x in removed:
				changed = True
				self.mutableList.removeService(eServiceReference(x))
			for x in added:
				changed = True
				self.mutableList.addService(eServiceReference(x))
			if changed:
				if self.bouquet_mark_edit == EDIT_ALTERNATIVES and not new_marked and self.__marked:
					self.mutableList.addService(eServiceReference(self.__marked[0]))
				self.mutableList.flushChanges()
		self.__marked = []
		self.clearMarks()
		self.bouquet_mark_edit = EDIT_OFF
		self.mutableList = None
		self.function = EDIT_OFF
		self.buildTitle()
		# NOTE: self.servicePath is just a reference to servicePathTv or Radio so we never ever use the assignment operator in self.servicePath!
		del self.servicePath[:]  # Remove all elements.
		self.servicePath += self.savedPath  # Add saved elements.
		del self.savedPath
		self.setRoot(self.servicePath[-1])
		if self.current_ref:
			self.setCurrentSelection(self.current_ref)
			self.current_ref = None

	def clearMarks(self):
		self.servicelist.clearMarks()

	def doMark(self):
		ref = self.servicelist.getCurrent()
		if self.servicelist.isMarked(ref):
			self.servicelist.removeMarked(ref)
		else:
			self.servicelist.addMarked(ref)

	def removeCurrentEntry(self, bouquet=False):
		if self.confirmRemove:
			choiceList = [
				(_("Yes"), True),
				(_("No"), False),
				(_("Yes, and don't ask again for this session"), "never")
			]
			self.session.openWithCallback(boundFunction(self.removeCurrentEntryCallback, bouquet), MessageBox, _("Are you sure to remove this entry?"), list=choiceList)
		else:
			self.removeCurrentEntryCallback(bouquet, True)

	def removeCurrentEntryCallback(self, bouquet, answer):
		if answer:
			if answer == "never":
				self.confirmRemove = False
			if bouquet:
				self.removeBouquet()
			else:
				self.removeCurrentService()

	def removeCurrentService(self, bouquet=False):
		self.editMode = True
		ref = self.servicelist.getCurrent()
		mutableList = self.getMutableList()
		if ref.valid() and mutableList is not None:
			if not mutableList.removeService(ref):
				mutableList.flushChanges()  # FIXME: Don't flush on each single removed service.
				self.servicelist.removeCurrent()
				self.servicelist.resetRoot()
				playingref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
				if not bouquet and playingref and ref == playingref:
					try:
						doClose = not config.usage.servicelistpreview_mode.value or ref == self.session.nav.getCurrentlyPlayingServiceOrGroup()
					except Exception:
						doClose = False
					if self.startServiceRef is None and not doClose:
						self.startServiceRef = self.session.nav.getCurrentlyPlayingServiceOrGroup()
					ref = self.getCurrentSelection()
					if self.movemode and (self.isBasePathEqual(self.bouquet_root) or "userbouquet." in ref.toString()):
						self.toggleMoveMarked()
					elif (ref.flags & eServiceReference.flagDirectory) == eServiceReference.flagDirectory:
						if parentalControl.isServicePlayable(ref, self.bouquetParentalControlCallback, self.session):
							self.enterPath(ref)
							self.gotoCurrentServiceOrProvider(ref)
					elif self.bouquet_mark_edit != EDIT_OFF:
						if not (self.bouquet_mark_edit == EDIT_ALTERNATIVES and ref.flags & eServiceReference.isGroup):
							self.doMark()
					elif not (ref.flags & eServiceReference.isMarker or ref.type == -1):
						root = self.getRoot()
						if not root or not (root.flags & eServiceReference.isGroup):
							self.zap(enable_pipzap=doClose, preview_zap=not doClose)
							self.asciiOff()

	def addServiceToBouquet(self, dest, service=None):
		mutableList = self.getMutableList(dest)
		if mutableList is not None:
			if service is None:  # Use current selected service.
				service = self.servicelist.getCurrent()
			if not mutableList.addService(service):
				mutableList.flushChanges()
				# Do some voodoo to check if current_root is equal to dest.
				cur_root = self.getRoot()
				str1 = cur_root and cur_root.toString() or -1
				str2 = dest.toString()
				pos1 = str1.find("FROM BOUQUET")
				pos2 = str2.find("FROM BOUQUET")
				if pos1 != -1 and pos2 != -1 and str1[pos1:] == str2[pos2:]:
					self.servicelist.addService(service)
				self.servicelist.resetRoot()

	def toggleMoveMode(self, select=False):
		self.editMode = True
		if self.movemode:
			if self.entry_marked:
				self.toggleMoveMarked()  # Unmark current entry.
			self.movemode = False
			self.mutableList.flushChanges()  # FIXME: Add check if changes was made.
			self.mutableList = None
			self.function = EDIT_OFF
			self.buildTitle()
			print("[ChannelSelection] toggleMoveMode DEBUG: Setting title='%s'." % self.getTitle())
			self.servicelist.resetRoot()
			self.servicelist.l.setHideNumberMarker(config.usage.hide_number_markers.value)
			self.servicelist.setCurrent(self.servicelist.getCurrent())
		else:
			self.mutableList = self.getMutableList()
			self.movemode = True
			select and self.toggleMoveMarked()
			self.function = EDIT_MOVE
			self.buildTitle()
			print("[ChannelSelection] toggleMoveMode DEBUG: Setting title='%s'." % self.getTitle())
			self.servicelist.setCurrent(self.servicelist.getCurrent())
		self["Service"].editmode = True

	def handleEditCancel(self):
		if self.movemode:  # Move mode active?
			self.toggleMoveMode()  # Disable move mode.
		elif self.bouquet_mark_edit != EDIT_OFF:
			self.endMarkedEdit(True)  # Abort edit mode.

	def toggleMoveMarked(self):
		if self.entry_marked:
			self.servicelist.setCurrentMarked(False)
			self.entry_marked = False
			self.pathChangeDisabled = False  # Re-enable path change.
		else:
			self.servicelist.setCurrentMarked(True)
			self.entry_marked = True
			self.pathChangeDisabled = True  # No path change allowed in move mode.

	def doContext(self):
		self.session.openWithCallback(self.exitContext, ChannelContextMenu, self)

	def exitContext(self, close=False):
		l = self["list"]
		l.setFontsize()
		l.setItemsPerPage()
		l.setMode("MODE_TV")
		# l.setMode("MODE_TV") automatically sets "hide number marker" to
		# the config.usage.hide_number_markers.value so when we are in "move mode"
		# we need to force display of the markers here after l.setMode("MODE_TV")
		# has run. If l.setMode("MODE_TV") were ever removed above,
		# "self.servicelist.l.setHideNumberMarker(False)" could be moved
		# directly to the "else" clause of "def toggleMoveMode".
		if self.movemode:
			self.servicelist.l.setHideNumberMarker(False)
		if close:
			self.cancel()


class ChannelSelectionBase(Screen):
	def __init__(self, session):
		Screen.__init__(self, session)
		self["key_red"] = Button(_("All Services"))
		self["key_green"] = Button(_("Reception Lists"))
		self["key_yellow"] = Button(_("Providers"))
		self["key_blue"] = Button(_("Favorites"))
		self["list"] = ServiceList(self)
		self.servicelist = self["list"]
		self.numericalTextInput = NumericalTextInput(handleTimeout=False)
		self.servicePath = []
		self.servicePathTV = []
		self.servicePathRadio = []
		self.history = []
		self.rootChanged = False
		self.startRoot = None
		self.selectionNumber = ""
		self.clearNumberSelectionNumberTimer = eTimer()
		self.clearNumberSelectionNumberTimer.callback.append(self.clearNumberSelectionNumber)
		self.protectContextMenu = True
		self.dopipzap = False
		self.pathChangeDisabled = False
		self.movemode = False
		self.showSatDetails = False
		self["ChannelSelectBaseActions"] = NumberActionMap(["ChannelSelectBaseActions", "NumberActions", "InputAsciiActions"], {
			"showFavourites": self.showFavourites,
			"showAllServices": self.showAllServices,
			"showProviders": self.showProviders,
			"showSatellites": boundFunction(self.showSatellites, changeMode=True),
			"nextBouquet": self.nextBouquet,
			"prevBouquet": self.prevBouquet,
			"nextMarker": self.nextMarker,
			"prevMarker": self.prevMarker,
			"gotAsciiCode": self.keyAsciiCode,
			"keyUp": self.moveUp,
			"keyDown": self.moveDown,
			"1": self.keyNumberGlobal,
			"2": self.keyNumberGlobal,
			"3": self.keyNumberGlobal,
			"4": self.keyNumberGlobal,
			"5": self.keyNumberGlobal,
			"6": self.keyNumberGlobal,
			"7": self.keyNumberGlobal,
			"8": self.keyNumberGlobal,
			"9": self.keyNumberGlobal,
			"0": self.keyNumberGlobal
		}, prio=-1)  # This prio is needed to overwrite the left/right action for neutrino keymap
		self.mode = MODE_TV
		self.baseTitle = _("Channel Selection")
		self.function = EDIT_OFF
		self.getBouquetMode()
		self.onShown.append(self.applyKeyMap)

	def applyKeyMap(self):
		if config.usage.show_channel_jump_in_servicelist.value == "alpha":
			self.numericalTextInput.setUseableChars("abcdefghijklmnopqrstuvwxyz1234567890")
		else:
			self.numericalTextInput.setUseableChars("1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ")

	def setTvMode(self):
		self.mode = MODE_TV
		self.servicePath = self.servicePathTV
		self.getBouquetMode()
		self.buildTitle()

	def setRadioMode(self):
		self.mode = MODE_RADIO
		self.servicePath = self.servicePathRadio
		self.getBouquetMode()
		self.buildTitle()

	def getBouquetMode(self):
		if self.mode == MODE_TV:
			self.service_types_ref = service_types_tv_ref
			self.bouquet_root = eServiceReference(multibouquet_tv_ref if config.usage.multibouquet.value else singlebouquet_tv_ref)
		else:
			self.service_types_ref = service_types_radio_ref
			self.bouquet_root = eServiceReference(multibouquet_radio_ref if config.usage.multibouquet.value else singlebouquet_radio_ref)
		self.service_types = self.service_types_ref.toString()
		self.bouquet_rootstr = self.bouquet_root.toString()

	def buildTitle(self):
		def getServiceName(serviceReference):
			serviceNameTmp = ServiceReference(serviceReference).getServiceName()
			serviceName = serviceNameTmp.replace(_("(TV)") if self.mode == MODE_TV else _("(Radio)"), "").replace("  ", " ").strip()
			print("[ChannelSelection] buildTitle DEBUG: Service Name Before='%s', After='%s'." % (serviceNameTmp, serviceName))
			if "User - bouquets" in serviceName:
				return _("User - Bouquets")
			if not serviceName:
				servicePath = serviceReference.getPath()
				if "FROM PROVIDERS" in servicePath:
					return _("Providers")
				if "FROM SATELLITES" in servicePath:
					return _("Reception Lists")
				if "ORDER BY name" in servicePath:
					return _("All Services")
			elif serviceName == "favourites" and not config.usage.multibouquet.value:  # Translate single bouquet favourites
				return _("Favorites")
			return serviceName

		mode = _("TV") if self.mode == MODE_TV else _("Radio")
		title = self.baseTitle
		length = len(self.servicePath)
		if length > 0:
			title = getServiceName(self.servicePath[0])
			if length > 1:
				reference = self.servicePath[length - 1]
				if reference:
					title = getServiceName(reference)
		functionType = {
			EDIT_ALTERNATIVES: _("Alternative Edit"),
			EDIT_BOUQUET: _("Bouquet Edit"),
			EDIT_FAVORITE: _("Favorite Edit"),
			EDIT_MOVE: _("Move Mode"),
			EDIT_PIP: _("PiP")
		}.get(self.function)
		functionType = " [%s]" % functionType if functionType else ""
		self.setTitle("%s - %s%s" % (mode, title, functionType))
		#self.setTitle("%s (%s)%s" % (title, mode, functionType))
		print("[ChannelSelection] buildTitle DEBUG: Setting title='%s'." % self.getTitle())

	def getServiceName(self, serviceReference):
		serviceNameTmp = ServiceReference(serviceReference).getServiceName()
		serviceName = serviceNameTmp.replace(_("(TV)") if self.mode == MODE_TV else _("(Radio)"), "").replace("  ", " ").strip()
		print("[ChannelSelection] getServiceName DEBUG: Service Name Before='%s', After='%s'." % (serviceNameTmp, serviceName))
		if "User - bouquets" in serviceName:
			return _("User - Bouquets")
		if not serviceName:
			servicePath = serviceReference.getPath()
			if "FROM PROVIDERS" in servicePath:
				return _("Providers")
			if "FROM SATELLITES" in servicePath:
				return _("Reception Lists")
			if "ORDER BY name" in servicePath:
				return _("All Services")
		return serviceName

	def setRoot(self, root, justSet=False):
		if self.startRoot is None:
			self.startRoot = self.getRoot()
		path = root.getPath()
		isBouquet = "FROM BOUQUET" in path and (root.flags & eServiceReference.isDirectory)
		inBouquetRootList = "FROM BOUQUET \"bouquets." in path  # FIXME: Hack.
		if not inBouquetRootList and isBouquet:
			self.servicelist.setMode(ServiceList.MODE_FAVOURITES)
		else:
			self.servicelist.setMode(ServiceList.MODE_NORMAL)
		self.servicelist.setRoot(root, justSet)
		self.rootChanged = True
		self.buildTitle()

	def moveUp(self):
		self.servicelist.moveUp()

	def moveDown(self):
		self.servicelist.moveDown()

	def moveTop(self):
		self.servicelist.moveTop()

	def moveEnd(self):
		self.servicelist.moveEnd()

	def clearPath(self):
		del self.servicePath[:]

	def enterPath(self, ref, justSet=False):
		self.servicePath.append(ref)
		self.setRoot(ref, justSet)

	def getBouquetNumOffset(self, bouquet):
		if not config.usage.multibouquet.value:
			return 0
		bStr = bouquet.toString()  # TODO Do we need this?
		offset = 0
		if "userbouquet." in bouquet.toCompareString():
			serviceHandler = eServiceCenter.getInstance()
			servicelist = serviceHandler.list(bouquet)
			if servicelist is not None:
				while True:
					serviceIterator = servicelist.getNext()
					if not serviceIterator.valid():  # Check if end of list.
						break
					number = serviceIterator.getChannelNum()
					if number > 0:
						offset = number - 1
						break
		return offset

	def enterUserbouquet(self, root, save_root=True):
		self.clearPath()
		self.getBouquetMode()
		if self.bouquet_root:
			self.enterPath(self.bouquet_root)
		self.enterPath(root)
		self.startRoot = None
		if save_root:
			self.saveRoot()

	def pathUp(self, justSet=False):
		prev = self.servicePath.pop()
		if self.servicePath:
			current = self.servicePath[-1]
			self.setRoot(current, justSet)
			if not justSet:
				self.setCurrentSelection(prev)
		return prev

	def isBasePathEqual(self, ref):
		if len(self.servicePath) > 1 and self.servicePath[0] == ref:
			return True
		return False

	def isPrevPathEqual(self, ref):
		length = len(self.servicePath)
		if length > 1 and self.servicePath[length - 2] == ref:
			return True
		return False

	def preEnterPath(self, refstr):
		return False

	def showAllServices(self):
		self["key_green"].setText(_("Reception Lists"))
		if not self.pathChangeDisabled:
			ref = serviceRefAppendPath(self.service_types_ref, "ORDER BY name")
			if not self.preEnterPath(ref.toString()):
				currentRoot = self.getRoot()
				if currentRoot is None or currentRoot != ref:
					self.clearPath()
					self.enterPath(ref)
					playingref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
					if playingref:
						self.setCurrentSelectionAlternative(playingref)

	def showSatellites(self, changeMode=False):
		if not self.pathChangeDisabled:
			ref = serviceRefAppendPath(self.service_types_ref, "FROM SATELLITES ORDER BY satellitePosition")
			if self.showSatDetails:
				self["key_green"].setText(_("Simple"))
			else:
				self["key_green"].setText(_("Extended"))
			if not self.preEnterPath(ref.toString()):
				justSet = False
				prev = None
				if self.isBasePathEqual(ref):
					if self.isPrevPathEqual(ref):
						justSet = True
					prev = self.pathUp(justSet)
				else:
					currentRoot = self.getRoot()
					if currentRoot is None or currentRoot != ref:
						justSet = True
						self.clearPath()
						self.enterPath(ref, True)
					if changeMode and currentRoot and currentRoot == ref:
						self.showSatDetails = not self.showSatDetails
						justSet = True
						self.clearPath()
						self.enterPath(ref, True)
						if self.showSatDetails:
							self["key_green"].setText(_("Simple"))
						else:
							self["key_green"].setText(_("Extended"))
				if justSet:
					addCableAndTerrestrialLater = []
					serviceHandler = eServiceCenter.getInstance()
					servicelist = serviceHandler.list(ref)
					if servicelist is not None:
						while True:
							service = servicelist.getNext()
							if not service.valid():  # Check if end of list.
								break
							unsigned_orbpos = service.getUnsignedData(4) >> 16
							orbpos = service.getData(4) >> 16
							if orbpos < 0:
								orbpos += 3600
							if "FROM PROVIDER" in service.getPath():
								service_type = self.showSatDetails and _("Providers")
							elif ("flags == %d" % (FLAG_SERVICE_NEW_FOUND)) in service.getPath():
								service_type = self.showSatDetails and _("New")
							else:
								service_type = _("Services")
							if service_type:
								if unsigned_orbpos == 0xFFFF:  # Cable.
									service_name = _("Cable")
									addCableAndTerrestrialLater.append(("%s - %s" % (service_name, service_type), service.toString()))
								elif unsigned_orbpos == 0xEEEE:  # Terrestrial.
									service_name = _("Terrestrial")
									addCableAndTerrestrialLater.append(("%s - %s" % (service_name, service_type), service.toString()))
								else:
									try:
										service_name = str(nimmanager.getSatDescription(orbpos))
									except Exception:
										if orbpos > 1800:  # West.
											orbpos = 3600 - orbpos
											h = _("W")
										else:
											h = _("E")
										service_name = ("%d.%d" + h) % (orbpos / 10, orbpos % 10)
									service.setName("%s - %s" % (service_name, service_type))
									self.servicelist.addService(service)
						cur_ref = self.session.nav.getCurrentlyPlayingServiceReference()
						self.servicelist.l.sort()
						if cur_ref:
							# pos = self.service_types.rfind(":")  # DEBUG NOTE: This doesn't appear to be used.
							ref = eServiceReference(self.service_types_ref)
							path = "(channelID == %08x%04x%04x) && %s ORDER BY name" % (
								cur_ref.getUnsignedData(4),  # Name space.
								cur_ref.getUnsignedData(2),  # TSID.
								cur_ref.getUnsignedData(3),  # ONID.
								self.service_types_ref.getPath())
							ref.setPath(path)
							ref.setName(_("Current transponder"))
							self.servicelist.addService(ref, beforeCurrent=True)
						for (service_name, service_ref) in addCableAndTerrestrialLater:
							ref = eServiceReference(service_ref)
							ref.setName(service_name)
							self.servicelist.addService(ref, beforeCurrent=True)
						self.servicelist.l.FillFinished()
						if prev is not None:
							self.setCurrentSelection(prev)
						elif cur_ref:
							op = cur_ref.getUnsignedData(4)
							if op >= 0xffff:
								hop = op >> 16
								if op >= 0x10000000 and (op & 0xffff):
									op &= 0xffff0000
								path = "(satellitePosition == %d) && %s ORDER BY name" % (hop, self.service_types_ref.getPath())
								ref = eServiceReference(eServiceReference.idDVB, eServiceReference.flagDirectory, path)
								ref.setUnsignedData(4, op)
								self.setCurrentSelectionAlternative(ref)

	def showProviders(self):
		self["key_green"].setText(_("Reception Lists"))
		if not self.pathChangeDisabled:
			ref = serviceRefAppendPath(self.service_types_ref, " FROM PROVIDERS ORDER BY name")
			if not self.preEnterPath(ref.toString()):
				if self.isBasePathEqual(ref):
					self.pathUp()
				else:
					currentRoot = self.getRoot()
					if currentRoot is None or currentRoot != ref:
						self.clearPath()
						self.enterPath(ref)
						service = self.session.nav.getCurrentService()
						if service:
							info = service.info()
							if info:
								provider = info.getInfoString(iServiceInformation.sProvider)
								ref = eServiceReference(eServiceReference.idDVB, eServiceReference.flagDirectory)
								ref.setPath("(provider == \"%s\") && %s ORDER BY name" % (provider, self.service_types_ref.getPath()))
								ref.setName(provider)
								self.setCurrentSelectionAlternative(ref)

	def changeBouquet(self, direction):
		if not self.pathChangeDisabled:
			if len(self.servicePath) > 1:
				ref = serviceRefAppendPath(self.service_types_ref, " FROM SATELLITES ORDER BY satellitePosition")
				if self.isBasePathEqual(ref):
					self.showSatellites()
				else:
					self.pathUp()
				if direction < 0:
					self.moveUp()
				else:
					self.moveDown()
				ref = self.getCurrentSelection()
				self.enterPath(ref)
				prev = None
				root = self.getRoot()
				for path in self.history:
					if len(path) > 2 and path[1] == root:
						prev = path[2]
				if prev is not None:
					self.setCurrentSelection(prev)

	def inBouquet(self):
		if self.servicePath and self.servicePath[0] == self.bouquet_root:
			return True
		return False

	def atBegin(self):
		return self.servicelist.atBegin()

	def atEnd(self):
		return self.servicelist.atEnd()

	def nextBouquet(self):
		if "reverseB" in config.usage.servicelist_cursor_behavior.value:
			if config.usage.channelbutton_mode.value == "0" or config.usage.channelbutton_mode.value == "3":
				self.changeBouquet(-1)
			else:
				self.servicelist.moveDown()
		else:
			if config.usage.channelbutton_mode.value == "0" or config.usage.channelbutton_mode.value == "3":
				self.changeBouquet(+1)
			else:
				self.servicelist.moveUp()

	def prevBouquet(self):
		if "reverseB" in config.usage.servicelist_cursor_behavior.value:
			if config.usage.channelbutton_mode.value == "0" or config.usage.channelbutton_mode.value == "3":
				self.changeBouquet(+1)
			else:
				self.servicelist.moveUp()
		else:
			if config.usage.channelbutton_mode.value == "0" or config.usage.channelbutton_mode.value == "3":
				self.changeBouquet(-1)
			else:
				self.servicelist.moveDown()

	def showFavourites(self):
		self["key_green"].setText(_("Reception Lists"))
		if not self.pathChangeDisabled:
			if not self.preEnterPath(self.bouquet_root.toString()):
				if self.isBasePathEqual(self.bouquet_root):
					self.pathUp()
				else:
					currentRoot = self.getRoot()
					if currentRoot is None or currentRoot != self.bouquet_root:
						self.clearPath()
						self.enterPath(self.bouquet_root)

	def keyNumberGlobal(self, number):
		if config.usage.show_channel_jump_in_servicelist.value == "quick":
			if self.isBasePathEqual(self.bouquet_root):
				if hasattr(self, "editMode") and self.editMode:
					if number == 2:
						self.renameEntry()
					if number == 6:
						self.toggleMoveMode(select=True)
					if number == 8:
						self.removeCurrentEntry(bouquet=False)
				else:
					self.numberSelectionActions(number)
			else:
				current_root = self.getRoot()
				if current_root and "FROM BOUQUET \"bouquets." in current_root.getPath():
					if hasattr(self, "editMode") and self.editMode:
						if number == 2:
							self.renameEntry()
						if number == 6:
							self.toggleMoveMode(select=True)
						if number == 8:
							self.removeCurrentEntry(bouquet=True)
					else:
						self.numberSelectionActions(number)
				else:
					charstr = self.numericalTextInput.getKey(number)
					if len(charstr) == 1:
						self.servicelist.moveToChar(charstr[0])
		else:
			charstr = self.numericalTextInput.getKey(number)
			if len(charstr) == 1:
				self.servicelist.moveToChar(charstr[0])

	def numberSelectionActions(self, number):
		if not (hasattr(self, "movemode") and self.movemode):
			if len(self.selectionNumber) > 4:
				self.clearNumberSelectionNumber()
			self.selectionNumber = self.selectionNumber + str(number)
			ref, bouquet = Screens.InfoBar.InfoBar.instance.searchNumber(int(self.selectionNumber), bouquet=self.getRoot())
			if ref:
				if not ref.flags & eServiceReference.isMarker:
					self.enterUserbouquet(bouquet, save_root=False)
					self.servicelist.setCurrent(ref)
				self.clearNumberSelectionNumberTimer.start(1000, True)
			else:
				self.clearNumberSelectionNumber()

	def clearNumberSelectionNumber(self):
		self.clearNumberSelectionNumberTimer.stop()
		self.selectionNumber = ""

	def keyAsciiCode(self):
		charstr = chr(getPrevAsciiCode())
		if len(charstr) == 1:
			self.servicelist.moveToChar(charstr[0])

	def getRoot(self):
		return self.servicelist.getRoot()

	def getCurrentSelection(self):
		return self.servicelist.getCurrent()

	def setCurrentSelection(self, service):
		if service:
			self.servicelist.setCurrent(service, adjust=False)

	def setCurrentSelectionAlternative(self, ref):
		if self.bouquet_mark_edit == EDIT_ALTERNATIVES and not (ref.flags & eServiceReference.isDirectory):
			for markedService in self.servicelist.getMarked():
				markedService = eServiceReference(markedService)
				self.setCurrentSelection(markedService)
				if markedService == self.getCurrentSelection():
					return
		self.setCurrentSelection(ref)

	def getBouquetList(self):
		bouquets = []
		serviceHandler = eServiceCenter.getInstance()
		if config.usage.multibouquet.value:
			list = serviceHandler.list(self.bouquet_root)
			if list:
				while True:
					s = list.getNext()
					if not s.valid():
						break
					if s.flags & eServiceReference.isDirectory and not s.flags & eServiceReference.isInvisible:
						info = serviceHandler.info(s)
						if info:
							bouquets.append((info.getName(s), s))
				return bouquets
		else:
			info = serviceHandler.info(self.bouquet_root)
			if info:
				bouquets.append((info.getName(self.bouquet_root), self.bouquet_root))
			return bouquets
		return None

	def keyGoUp(self):
		if len(self.servicePath) > 1:
			if self.isBasePathEqual(self.bouquet_root):
				self.showFavourites()
			else:
				ref = serviceRefAppendPath(self.service_types_ref, " FROM SATELLITES ORDER BY satellitePosition")
				if self.isBasePathEqual(ref):
					self.showSatellites()
				else:
					ref = serviceRefAppendPath(self.service_types_ref, " FROM PROVIDERS ORDER BY name")
					if self.isBasePathEqual(ref):
						self.showProviders()
					else:
						self.showAllServices()

	def nextMarker(self):
		self.servicelist.moveToNextMarker()

	def prevMarker(self):
		self.servicelist.moveToPrevMarker()

	def gotoCurrentServiceOrProvider(self, ref):
		if _("Providers") in ref.getName():
			service = self.session.nav.getCurrentService()
			if service:
				info = service.info()
				if info:
					provider = info.getInfoString(iServiceInformation.sProvider)
					op = self.session.nav.getCurrentlyPlayingServiceOrGroup().getUnsignedData(4) >> 16
					ref = eServiceReference(eServiceReference.idDVB, eServiceReference.flagDirectory)
					ref.setPath("(provider == \"%s\") && (satellitePosition == %d) && %s ORDER BY name" % (provider, op, self.service_types_ref.getPath()))
					ref.setName(provider)
					self.servicelist.setCurrent(eServiceReference(ref))
		elif not self.isBasePathEqual(self.bouquet_root) or self.bouquet_mark_edit == EDIT_ALTERNATIVES:
			playingref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
			if playingref:
				self.setCurrentSelectionAlternative(playingref)


class ChannelContextMenu(Screen, HelpableScreen):
	def __init__(self, session, csel):
		def appendWhenValid(current, menu, args, level=0, key="bullet"):
			if current and current.valid() and level <= config.usage.setup_level.index:
				menu.append(ChoiceEntryComponent(key, args))

		Screen.__init__(self, session)
		HelpableScreen.__init__(self)
		self.setTitle(_("Channel List Context Menu"))
		# raise Exception("[ChannelSelection] We need a better summary screen here!")
		self.csel = csel
		self.bsel = None
		if self.isProtected():
			self.onFirstExecBegin.append(boundFunction(self.session.openWithCallback, self.protectResult, PinInput, pinList=[x.value for x in config.ParentalControl.servicepin], triesEntry=config.ParentalControl.retries.servicepin, title=_("Please enter the correct pin code"), windowTitle=_("Enter pin code")))
		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("Select"))
		self["key_yellow"] = StaticText("")
		self["key_blue"] = StaticText("")
		self["actions"] = HelpableActionMap(self, ["SelectCancelActions", "ColorActions", "NumberActions", "MenuActions"], {
			"cancel": (self.keyCancel, _("Cancel and exit the context menu")),
			"select": (self.keySelect, _("Select the currently highlighted action")),
			"menu": (self.keySetup, _("Open the Channel Selection Settings screen")),
			"0": (self.reloadServices, _("Reload all services from disk")),
			"1": (self.showBouquetInputBox, _("Add a bouquet")),
			"2": (self.renameEntry, _("Rename selected service")),
			"3": (self.findCurrentlyPlayed, _("Find the service currently playing")),
			# "4": Available for use.
			"5": (self.addServiceToBouquetOrAlternative, _("Add selected service to bouquet or alternative")),
			"6": (self.toggleMoveModeSelect, _("Toggle move mode selection")),
			"7": (self.showMarkerInputBox, _("Add a marker")),
			"8": (self.removeEntry, _("Remove selected service"))
			# "9": Available for use.
		}, prio=0, description=_("Channel List Context Menu Actions"))
		self["mainAction"] = HelpableActionMap(self, ["ColorActions"], {
			"yellow": (self.playMain, _("Play selected service on the main screen"))
		}, prio=0, description=_("Channel List Context Menu Actions"))
		self["mainAction"].setEnabled(False)
		self["pipAction"] = HelpableActionMap(self, ["ColorActions"], {
			"blue": (self.showServiceInPiP, _("Play selected service in a PiP window"))
		}, prio=0, description=_("Channel List Context Menu Actions"))
		self["pipAction"].setEnabled(False)
		self["navigationActions"] = HelpableActionMap(self, ["NavigationActions"], {
			"top": (self.keyTop, _("Move to the first line / screen")),
			"pageUp": (self.keyPageUp, _("Move up a screen")),
			"up": (self.keyUp, _("Move up a line")),
			"down": (self.keyDown, _("Move down a line")),
			"pageDown": (self.keyPageDown, _("Move down a screen")),
			"bottom": (self.keyBottom, _("Move to the last line / screen"))
		}, prio=0, description=_("Channel List Context Menu Navigation Actions"))
		self.removeFunction = False
		self.addFunction = False
		self.pipAvailable = False
		current = csel.getCurrentSelection()
		current_root = csel.getRoot()
		current_sel_path = current.getPath()
		current_sel_flags = current.flags
		inBouquetRootList = current_root and "FROM BOUQUET \"bouquets." in current_root.getPath()  # FIXME: Hack.
		inAlternativeList = current_root and "FROM BOUQUET \"alternatives" in current_root.getPath()
		self.inBouquet = csel.getMutableList() is not None
		haveBouquets = config.usage.multibouquet.value
		self.parentalControlEnabled = config.ParentalControl.servicepinactive.value
		menu = []
		menu.append(ChoiceEntryComponent(key="menu", text=(_("Settings..."), boundFunction(self.keySetup))))
		if not (current_sel_path or current_sel_flags & (eServiceReference.isDirectory | eServiceReference.isMarker)):
			appendWhenValid(current, menu, (_("Show Transponder Information"), self.showServiceInformations), level=2)
		if csel.bouquet_mark_edit == EDIT_OFF and not csel.entry_marked:
			if not inBouquetRootList:
				isPlayable = not (current_sel_flags & (eServiceReference.isMarker | eServiceReference.isDirectory))
				if isPlayable:
					for p in plugins.getPlugins(PluginDescriptor.WHERE_CHANNEL_CONTEXT_MENU):
						appendWhenValid(current, menu, (p.name, boundFunction(self.runPlugin, p)))
					if config.servicelist.startupservice.value == self.csel.getCurrentSelection().toString():
						appendWhenValid(current, menu, (_("Unset As Startup Service"), self.unsetStartupService))
					else:
						appendWhenValid(current, menu, (_("Set As Startup Service"), self.setStartupService))
					if self.parentalControlEnabled:
						if parentalControl.getProtectionLevel(csel.getCurrentSelection().toCompareString()) == -1:
							appendWhenValid(current, menu, (_("Add To Parental Protection"), boundFunction(self.addParentalProtection, csel.getCurrentSelection())))
						else:
							appendWhenValid(current, menu, (_("Remove From Parental Protection"), boundFunction(self.removeParentalProtection, csel.getCurrentSelection())))
						if config.ParentalControl.hideBlacklist.value and not parentalControl.sessionPinCached and config.ParentalControl.storeservicepin.value != "never":
							appendWhenValid(current, menu, (_("Unhide Parental Control Services"), boundFunction(self.unhideParentalServices)))
					if BoxInfo.getItem("3DMode"):
						if eDVBDB.getInstance().getFlag(eServiceReference(current.toString())) & FLAG_IS_DEDICATED_3D:
							appendWhenValid(current, menu, (_("Unmark As Dedicated 3D Service"), self.removeDedicated3DFlag))
						else:
							appendWhenValid(current, menu, (_("Mark As Dedicated 3D Service"), self.addDedicated3DFlag))
					if Screens.InfoBar.InfoBar.instance.checkStreamrelay(current):
						appendWhenValid(current, menu, (_("Play service without Stream Relay"), self.toggleStreamrelay))
					else:
						appendWhenValid(current, menu, (_("Play service with Stream Relay"), self.toggleStreamrelay))

					if eDVBDB.getInstance().getFlag(eServiceReference(current.toString())) & FLAG_HIDE_VBI:
						appendWhenValid(current, menu, (_("Show VBI Line For This Service"), self.removeHideVBIFlag))
					else:
						appendWhenValid(current, menu, (_("Hide VBI Line For This Service"), self.addHideVBIFlag))
					if eDVBDB.getInstance().getFlag(eServiceReference(current.toString())) & FLAG_CENTER_DVB_SUBS:
						appendWhenValid(current, menu, (_("Don't Center DVB Subs On This Service"), self.removeCenterDVBSubsFlag))
					else:
						appendWhenValid(current, menu, (_("Center DVB Subs On This Service"), self.addCenterDVBSubsFlag))
					if haveBouquets:
						bouquets = self.csel.getBouquetList()
						if bouquets is None:
							bouquetCnt = 0
						else:
							bouquetCnt = len(bouquets)
						if not self.inBouquet or bouquetCnt > 1:
							appendWhenValid(current, menu, (_("Add Service To Bouquet"), self.addServiceToBouquetSelected), key="5")
							self.addFunction = self.addServiceToBouquetSelected
						if not self.inBouquet:
							appendWhenValid(current, menu, (_("Remove Entry"), self.removeEntry), key="8")
							self.removeFunction = self.removeSatelliteService
					else:
						if not self.inBouquet:
							appendWhenValid(current, menu, (_("Add Service To Favorites"), self.addServiceToBouquetSelected), key="5")
							self.addFunction = self.addServiceToBouquetSelected
					if BoxInfo.getItem("PIPAvailable"):
						if not self.parentalControlEnabled or parentalControl.getProtectionLevel(csel.getCurrentSelection().toCompareString()) == -1:
							if self.csel.dopipzap:
								appendWhenValid(current, menu, (_("Play In Main window"), self.playMain), key="yellow")
								self["key_yellow"].setText(_("Play in Main"))
								self["key_blue"].setText("")
								self["mainAction"].setEnabled(True)
								self["pipAction"].setEnabled(False)
							else:
								appendWhenValid(current, menu, (_("Play As Picture in Picture window"), self.showServiceInPiP), key="blue")
								self["key_yellow"].setText("")
								self["key_blue"].setText(_("Play in PiP"))
								self["mainAction"].setEnabled(False)
								self["pipAction"].setEnabled(True)
					appendWhenValid(current, menu, (_("Find Currently Playing Service"), self.findCurrentlyPlayed), key="3")
				else:
					if "FROM SATELLITES" in current_root.getPath() and current and _("Services") in eServiceCenter.getInstance().info(current).getName(current):
						unsigned_orbpos = current.getUnsignedData(4) >> 16
						if unsigned_orbpos == 0xFFFF:
							appendWhenValid(current, menu, (_("Remove Cable Services"), self.removeSatelliteServices))
						elif unsigned_orbpos == 0xEEEE:
							appendWhenValid(current, menu, (_("Remove Terrestrial Services"), self.removeSatelliteServices))
						else:
							appendWhenValid(current, menu, (_("Remove Satellite Services"), self.removeSatelliteServices))
					if haveBouquets:
						if not self.inBouquet and "PROVIDERS" not in current_sel_path:
							appendWhenValid(current, menu, (_("Copy To Bouquets"), self.copyCurrentToBouquetList))
							appendWhenValid(current, menu, (_("Copy To Stream Relay"), self.copyCurrentToStreamRelay))
					if ("flags == %d" % (FLAG_SERVICE_NEW_FOUND)) in current_sel_path:
						appendWhenValid(current, menu, (_("Remove All New Found Flags"), self.removeAllNewFoundFlags))
				if self.inBouquet:
					appendWhenValid(current, menu, (_("Rename Entry"), self.renameEntry), key="2")
					if not inAlternativeList:
						appendWhenValid(current, menu, (_("Remove Entry"), self.removeEntry), key="8")
						self.removeFunction = self.removeCurrentService
				if current_root and ("flags == %d" % (FLAG_SERVICE_NEW_FOUND)) in current_root.getPath():
					appendWhenValid(current, menu, (_("Remove New Found Flag"), self.removeNewFoundFlag))
			else:
					if self.parentalControlEnabled:
						if parentalControl.getProtectionLevel(csel.getCurrentSelection().toCompareString()) == -1:
							appendWhenValid(current, menu, (_("Add Bouquet To Parental Protection"), boundFunction(self.addParentalProtection, csel.getCurrentSelection())))
						else:
							appendWhenValid(current, menu, (_("Remove Bouquet From Parental Protection"), boundFunction(self.removeParentalProtection, csel.getCurrentSelection())))
					menu.append(ChoiceEntryComponent(key="1", text=(_("Add Bouquet"), self.showBouquetInputBox)))
					appendWhenValid(current, menu, (_("Rename Entry"), self.renameEntry), key="2")
					appendWhenValid(current, menu, (_("Remove Entry"), self.removeEntry), key="8")
					self.removeFunction = self.removeBouquet
					if removed_userbouquets_available():
						appendWhenValid(current, menu, (_("Purge Deleted User Bouquets"), self.purgeDeletedBouquets))
						appendWhenValid(current, menu, (_("Restore Deleted User Bouquets"), self.restoreDeletedBouquets))
		if self.inBouquet:  # Current list is editable?
			if csel.bouquet_mark_edit == EDIT_OFF:
				if csel.movemode:
					appendWhenValid(current, menu, (_("Disable Move Mode"), self.toggleMoveMode), key="6")
				else:
					appendWhenValid(current, menu, (_("Enable Move Mode"), self.toggleMoveMode), level=1, key="6")
				if not csel.entry_marked and not inBouquetRootList and current_root and not (current_root.flags & eServiceReference.isGroup):
					if current.type != -1:
						menu.append(ChoiceEntryComponent(key="7", text=(_("Add Marker"), self.showMarkerInputBox)))
					if BoxInfo.getItem("HDMIin"):
						appendWhenValid(current, menu, (_("Add HDMI IN to bouquet"), self.showHDMIInInputBox))
					if not csel.movemode:
						if haveBouquets:
							appendWhenValid(current, menu, (_("Enable Bouquet Edit"), self.bouquetMarkStart))
						else:
							appendWhenValid(current, menu, (_("Enable Favorite Edit"), self.bouquetMarkStart))
					if current_sel_flags & eServiceReference.isGroup:
						appendWhenValid(current, menu, (_("Edit Alternatives"), self.editAlternativeServices), level=2)
						appendWhenValid(current, menu, (_("Show Alternatives"), self.showAlternativeServices), level=2)
						appendWhenValid(current, menu, (_("Remove All Alternatives"), self.removeAlternativeServices), level=2)
					elif not current_sel_flags & eServiceReference.isMarker:
						appendWhenValid(current, menu, (_("Add Alternatives"), self.addAlternativeServices), level=2)
			else:
				if csel.bouquet_mark_edit == EDIT_BOUQUET:
					if haveBouquets:
						appendWhenValid(current, menu, (_("End Bouquet Edit"), self.bouquetMarkEnd))
						appendWhenValid(current, menu, (_("Abort Bouquet Edit"), self.bouquetMarkAbort))
					else:
						appendWhenValid(current, menu, (_("End Favorites Edit"), self.bouquetMarkEnd))
						appendWhenValid(current, menu, (_("Abort Favorites Edit"), self.bouquetMarkAbort))
					if current_sel_flags & eServiceReference.isMarker:
						appendWhenValid(current, menu, (_("Rename Entry"), self.renameEntry), key="2")
						appendWhenValid(current, menu, (_("Remove Entry"), self.removeEntry), key="8")
						self.removeFunction = self.removeCurrentService
				else:
					appendWhenValid(current, menu, (_("End Alternatives Edit"), self.bouquetMarkEnd))
					appendWhenValid(current, menu, (_("Abort Alternatives Edit"), self.bouquetMarkAbort))
		menu.append(ChoiceEntryComponent(key="9", text=(_("Reload Services"), self.reloadServices)))
		self["menu"] = ChoiceList(menu)
		self.onLayoutFinish.append(self.layoutFinished)

	def isProtected(self):
		return self.csel.protectContextMenu and config.ParentalControl.setuppinactive.value and config.ParentalControl.config_sections.context_menus.value

	def protectResult(self, answer):
		if answer:
			self.csel.protectContextMenu = False
		elif answer is not None:
			self.session.openWithCallback(self.close, MessageBox, _("The PIN code entered is incorrect!"), MessageBox.TYPE_ERROR)
		else:
			self.close()

	def layoutFinished(self):
		self["menu"].enableAutoNavigation(False)

	def keyCancel(self, dummy=False):
		self.close(False)

	def keySelect(self):
		self["menu"].getCurrent()[0][1]()

	def keySetup(self):
		self.session.openWithCallback(self.keyCancel, Setup, "ChannelSelection")

	def keyTop(self):
		self["menu"].goTop()

	def keyPageUp(self):
		self["menu"].goPageUp()

	def keyUp(self):
		self["menu"].goLineUp()

	def keyDown(self):
		self["menu"].goLineDown()

	def keyPageDown(self):
		self["menu"].goPageDown()

	def keyBottom(self):
		self["menu"].goBottom()

	def addDedicated3DFlag(self):
		eDVBDB.getInstance().addFlag(eServiceReference(self.csel.getCurrentSelection().toString()), FLAG_IS_DEDICATED_3D)
		eDVBDB.getInstance().reloadBouquets()
		self.set3DMode(True)
		self.close()

	def removeDedicated3DFlag(self):
		eDVBDB.getInstance().removeFlag(eServiceReference(self.csel.getCurrentSelection().toString()), FLAG_IS_DEDICATED_3D)
		eDVBDB.getInstance().reloadBouquets()
		self.set3DMode(False)
		self.close()

	def set3DMode(self, value):
		if config.osd.threeDmode.value == "auto" and self.session.nav.currentlyPlayingServiceReference == self.csel.getCurrentSelection():
			from Screens.VideoMode import applySettings  # This needs to be here as VideoMode has a circular import!
			applySettings(value and "sidebyside" or config.osd.threeDmode.value)

	def toggleStreamrelay(self):
		Screens.InfoBar.InfoBar.instance.ToggleStreamrelay(self.csel.getCurrentSelection())
		self.close()

	def addHideVBIFlag(self):
		eDVBDB.getInstance().addFlag(eServiceReference(self.csel.getCurrentSelection().toString()), FLAG_HIDE_VBI)
		eDVBDB.getInstance().reloadBouquets()
		self.close()

	def removeHideVBIFlag(self):
		eDVBDB.getInstance().removeFlag(eServiceReference(self.csel.getCurrentSelection().toString()), FLAG_HIDE_VBI)
		eDVBDB.getInstance().reloadBouquets()
		self.close()

	def addCenterDVBSubsFlag(self):
		eDVBDB.getInstance().addFlag(eServiceReference(self.csel.getCurrentSelection().toString()), FLAG_CENTER_DVB_SUBS)
		eDVBDB.getInstance().reloadBouquets()
		config.subtitles.dvb_subtitles_centered.value = True
		self.close()

	def removeCenterDVBSubsFlag(self):
		eDVBDB.getInstance().removeFlag(eServiceReference(self.csel.getCurrentSelection().toString()), FLAG_CENTER_DVB_SUBS)
		eDVBDB.getInstance().reloadBouquets()
		config.subtitles.dvb_subtitles_centered.value = False
		self.close()

	def addServiceToBouquetOrAlternative(self):
		if self.addFunction:
			self.addFunction()
		else:
			return 0

	def getCurrentSelectionName(self):
		cur = self.csel.getCurrentSelection()
		if cur and cur.valid():
			name = eServiceCenter.getInstance().info(cur).getName(cur) or ServiceReference(cur).getServiceName() or ""
			name = name.replace("\xc2\x86", "").replace("\xc2\x87", "")
			return name
		return ""

	def removeEntry(self):
		ref = self.csel.servicelist.getCurrent()
		if self.removeFunction and ref and ref.valid():
			if self.csel.confirmRemove:
				choiceList = [
					(_("Yes"), True),
					(_("No"), False),
					(_("Yes, and don't ask again for this session"), "never")
				]
				self.session.openWithCallback(self.removeFunction, MessageBox, "%s\n%s" % (_("Are you sure to remove this entry?"), self.getCurrentSelectionName()), list=choiceList)
			else:
				self.removeFunction(True)
		else:
			return 0

	def removeCurrentService(self, answer):
		if answer:
			if answer == "never":
				self.csel.confirmRemove = False
			self.csel.removeCurrentService()
			self.close()

	def removeSatelliteService(self, answer):
		if answer:
			if answer == "never":
				self.csel.confirmRemove = False
			self.csel.removeSatelliteService()
			self.close()

	def removeBouquet(self, answer):
		if answer:
			self.csel.removeBouquet()
			eDVBDB.getInstance().reloadBouquets()
			self.close()

	def purgeDeletedBouquets(self):
		self.session.openWithCallback(self.purgeDeletedBouquetsCallback, MessageBox, _("Are you sure to purge all deleted user bouquets?"))

	def purgeDeletedBouquetsCallback(self, answer):
		if answer:
			for file in listdir("/etc/enigma2/"):
				if file.startswith("userbouquet") and file.endswith(".del"):
					file = join("/etc/enigma2", file)
					print("[ChannelSelection] Permanently remove file '%s'." % file)
					remove(file)
			self.close()

	def restoreDeletedBouquets(self):
		for file in listdir("/etc/enigma2/"):
			if file.startswith("userbouquet") and file.endswith(".del"):
				file = join("/etc/enigma2", file)
				print("[ChannelSelection] Restore file '%s'." % file[:-4])
				rename(file, file[:-4])
		eDVBDBInstance = eDVBDB.getInstance()
		eDVBDBInstance.setLoadUnlinkedUserbouquets(True)
		eDVBDBInstance.reloadBouquets()
		eDVBDBInstance.setLoadUnlinkedUserbouquets(config.misc.load_unlinked_userbouquets.value)
		refreshServiceList()
		self.csel.showFavourites()
		self.close()

	def playMain(self):
		sel = self.csel.getCurrentSelection()
		if sel and sel.valid() and self.csel.dopipzap and (not self.parentalControlEnabled or parentalControl.getProtectionLevel(self.csel.getCurrentSelection().toCompareString()) == -1):
			self.csel.zap()
			self.csel.setCurrentSelection(sel)
			self.close(True)
		else:
			return 0

	def reloadServices(self):
		eDVBDB.getInstance().reloadBouquets()
		eDVBDB.getInstance().reloadServicelist()
		self.session.openWithCallback(self.close, MessageBox, _("The service list is reloaded."), MessageBox.TYPE_INFO, timeout=5)

	def showServiceInformations(self):
		from Screens.Information import ServiceInformation  # The import needs to be here
		self.session.open(ServiceInformation, self.csel.getCurrentSelection())

	def setStartupService(self):
		self.session.openWithCallback(self.setStartupServiceCallback, MessageBox, _("Set startup service"), list=[(_("Only on startup"), "startup"), (_("Also on standby"), "standby")])

	def setStartupServiceCallback(self, answer):
		if answer:
			config.servicelist.startupservice.value = self.csel.getCurrentSelection().toString()
			path = ";".join([i.toString() for i in self.csel.servicePath])
			config.servicelist.startuproot.value = path
			config.servicelist.startupmode.value = config.servicelist.lastmode.value
			config.servicelist.startupservice_onstandby.value = answer == "standby"
			config.servicelist.save()
			configfile.save()
			self.close()

	def unsetStartupService(self):
		config.servicelist.startupservice.value = ""
		config.servicelist.startupservice_onstandby.value = False
		config.servicelist.save()
		configfile.save()
		self.close()

	def setStartupServiceStandby(self):
		config.servicelist.startupservice_standby.value = self.csel.getCurrentSelection().toString()
		config.servicelist.save()
		configfile.save()
		self.close()

	def unsetStartupServiceStandby(self):
		config.servicelist.startupservice_standby.value = ""
		config.servicelist.save()
		configfile.save()
		self.close()

	def showBouquetInputBox(self):
		self.session.openWithCallback(self.bouquetInputCallback, VirtualKeyBoard, title=_("Please enter a name for the new bouquet"), text="bouquetname", maxSize=False, visible_width=56, type=Input.TEXT)

	def bouquetInputCallback(self, bouquet):
		if bouquet is not None:
			self.csel.addBouquet(bouquet, None)
		self.close()

	def addParentalProtection(self, service):
		parentalControl.protectService(service.toCompareString())
		if config.ParentalControl.hideBlacklist.value and not parentalControl.sessionPinCached:
			self.csel.servicelist.resetRoot()
		self.close()

	def removeParentalProtection(self, service):
		self.session.openWithCallback(boundFunction(self.pinEntered, service.toCompareString()), PinInput, pinList=[config.ParentalControl.servicepin[0].value], triesEntry=config.ParentalControl.retries.servicepin, title=_("Enter the service pin"), windowTitle=_("Enter pin code"))

	def pinEntered(self, service, answer):
		if answer:
			parentalControl.unProtectService(service)
			self.close()
		elif answer is not None:
			self.session.openWithCallback(self.close, MessageBox, _("The PIN code entered is incorrect!"), MessageBox.TYPE_ERROR)
		else:
			self.close()

	def unhideParentalServices(self):
		if self.csel.protectContextMenu:
			self.session.openWithCallback(self.unhideParentalServicesCallback, PinInput, pinList=[config.ParentalControl.servicepin[0].value], triesEntry=config.ParentalControl.retries.servicepin, title=_("Enter the service pin"), windowTitle=_("Enter pin code"))
		else:
			self.unhideParentalServicesCallback(True)

	def unhideParentalServicesCallback(self, answer):
		if answer:
			service = self.csel.servicelist.getCurrent()
			parentalControl.setSessionPinCached()
			parentalControl.hideBlacklist()
			self.csel.servicelist.resetRoot()
			self.csel.servicelist.setCurrent(service)
			self.close()
		elif answer is not None:
			self.session.openWithCallback(self.close, MessageBox, _("The PIN code entered is incorrect!"), MessageBox.TYPE_ERROR)
		else:
			self.close()

	def showServiceInPiP(self):
		if self.csel.dopipzap or (self.parentalControlEnabled and not parentalControl.getProtectionLevel(self.csel.getCurrentSelection().toCompareString()) == -1):
			return 0
		service = self.session.nav.getCurrentService()
		info = service and service.info()
		xres = str(info.getInfo(iServiceInformation.sVideoWidth))
		if int(xres) <= 720 or BoxInfo.getItem("model") != "blackbox7405":
			if self.session.pipshown:
				del self.session.pip
				if BoxInfo.getItem("LCDMiniTVPiP") and int(config.lcd.modepip.value) >= 1:
					print("[ChannelSelection] LCDMiniTV disable PiP.")
					fileWriteLine("/proc/stb/lcd/mode", str(config.lcd.modeminitv.value))
			self.session.pip = self.session.instantiateDialog(PictureInPicture)
			self.session.pip.setAnimationMode(0)
			self.session.pip.show()
			newservice = self.csel.servicelist.getCurrent()
			currentBouquet = self.csel.servicelist and self.csel.servicelist.getRoot()
			if newservice and newservice.valid():
				if self.session.pip.playService(newservice):
					self.session.pipshown = True
					self.session.pip.servicePath = self.csel.getCurrentServicePath()
					self.session.pip.servicePath[1] = currentBouquet
					if BoxInfo.getItem("LCDMiniTVPiP") and int(config.lcd.modepip.value) >= 1:
						print("[ChannelSelection] LCDMiniTV enable PiP.")
						f = open("/proc/stb/lcd/mode", "w")
						f.write(config.lcd.modepip.value)
						f.close()
						f = open("/proc/stb/vmpeg/1/dst_width", "w")
						f.write("0")
						f.close()
						f = open("/proc/stb/vmpeg/1/dst_height", "w")
						f.write("0")
						f.close()
						f = open("/proc/stb/vmpeg/1/dst_apply", "w")
						f.write("1")
						f.close()
					self.close(True)
				else:
					self.session.pipshown = False
					del self.session.pip
					if BoxInfo.getItem("LCDMiniTV") and int(config.lcd.modepip.value) >= 1:
						print("[ChannelSelection] LCDMiniTV disable PiP.")
						fileWriteLine("/proc/stb/lcd/mode", str(config.lcd.modeminitv.value))
					self.session.openWithCallback(self.close, MessageBox, _("Could not open Picture in Picture"), MessageBox.TYPE_ERROR)
		else:
			self.session.open(MessageBox, _("Your %s %s does not support PiP HD") % getBoxDisplayName(), type=MessageBox.TYPE_INFO, timeout=5)

	def addServiceToBouquetSelected(self):
		bouquets = self.csel.getBouquetList()
		if bouquets is None:
			cnt = 0
		else:
			cnt = len(bouquets)
		if cnt > 1:  # Show bouquet list.
			self.bsel = self.session.openWithCallback(self.bouquetSelClosed, BouquetSelector, bouquets, self.addCurrentServiceToBouquet)
		elif cnt == 1:  # Add to only one existing bouquet.
			self.addCurrentServiceToBouquet(bouquets[0][1], closeBouquetSelection=False)

	def bouquetSelClosed(self, recursive):
		self.bsel = None
		if recursive:
			self.close(False)

	def removeSatelliteServices(self):
		self.csel.removeSatelliteServices()
		self.close()

	def copyCurrentToBouquetList(self):
		self.csel.copyCurrentToBouquetList()
		self.close()

	def copyCurrentToStreamRelay(self):
		self.csel.copyCurrentToStreamRelay()
		self.close()

	def showHDMIInInputBox(self):
		self.session.openWithCallback(self.hdmiInputCallback, VirtualKeyBoard, title=_("Please enter a name for the HDMI-IN"), text="HDMI-IN", maxSize=False, visible_width=56, type=Input.TEXT)

	def hdmiInputCallback(self, marker):
		if marker is not None:
			self.csel.addHDMIIn(marker)
		self.close()

	def showMarkerInputBox(self):
		self.session.openWithCallback(self.markerInputCallback, VirtualKeyBoard, title=_("Please enter a name for the new marker"), text="markername", maxSize=False, visible_width=56, type=Input.TEXT)

	def markerInputCallback(self, marker):
		if marker is not None:
			self.csel.addMarker(marker)
		self.close()

	def addCurrentServiceToBouquet(self, dest, closeBouquetSelection=True):
		self.csel.addServiceToBouquet(dest)
		if self.bsel is not None:
			self.bsel.close(True)
		else:
			self.close(closeBouquetSelection)  # Close bouquet selection.

	def renameEntry(self):
		if self.inBouquet and self.csel.servicelist.getCurrent() and self.csel.servicelist.getCurrent().valid() and not self.csel.entry_marked:
			self.csel.renameEntry()
			self.close()
		else:
			return 0

	def toggleMoveMode(self):
		if self.inBouquet and self.csel.servicelist.getCurrent() and self.csel.servicelist.getCurrent().valid():
			self.csel.toggleMoveMode()
			self.close()
		else:
			return 0

	def toggleMoveModeSelect(self):
		if self.inBouquet and self.csel.servicelist.getCurrent() and self.csel.servicelist.getCurrent().valid():
			self.csel.toggleMoveMode(True)
			self.close()
		else:
			return 0

	def bouquetMarkStart(self):
		self.csel.startMarkedEdit(EDIT_BOUQUET)
		self.close()

	def bouquetMarkEnd(self):
		self.csel.endMarkedEdit(abort=False)
		self.close()

	def bouquetMarkAbort(self):
		self.csel.endMarkedEdit(abort=True)
		self.close()

	def removeNewFoundFlag(self):
		eDVBDB.getInstance().removeFlag(self.csel.getCurrentSelection(), FLAG_SERVICE_NEW_FOUND)
		self.close()

	def removeAllNewFoundFlags(self):
		curpath = self.csel.getCurrentSelection().getPath()
		idx = curpath.find("satellitePosition == ")
		if idx != -1:
			tmp = curpath[idx + 21:]
			idx = tmp.find(")")
			if idx != -1:
				satpos = int(tmp[:idx])
				eDVBDB.getInstance().removeFlags(FLAG_SERVICE_NEW_FOUND, -1, -1, -1, satpos)
		self.close()

	def editAlternativeServices(self):
		self.csel.startMarkedEdit(EDIT_ALTERNATIVES)
		self.close()

	def showAlternativeServices(self):
		self.csel["Service"].editmode = True
		self.csel.enterPath(self.csel.getCurrentSelection())
		self.close()

	def removeAlternativeServices(self):
		self.csel.removeAlternativeServices()
		self.close()

	def addAlternativeServices(self):
		self.csel.addAlternativeServices()
		self.csel.startMarkedEdit(EDIT_ALTERNATIVES)
		self.close()

	def findCurrentlyPlayed(self):
		sel = self.csel.getCurrentSelection()
		if sel and sel.valid() and not self.csel.entry_marked:
			currentPlayingService = (hasattr(self.csel, "dopipzap") and self.csel.dopipzap) and self.session.pip.getCurrentService() or self.session.nav.getCurrentlyPlayingServiceOrGroup()
			self.csel.servicelist.setCurrent(currentPlayingService, adjust=False)
			if self.csel.getCurrentSelection() != currentPlayingService:
				self.csel.setCurrentSelection(sel)
			self.close()
		else:
			return 0

	def runPlugin(self, plugin):
		plugin(session=self.session, service=self.csel.getCurrentSelection())
		self.close()


# Configuration for last service:
config.tv = ConfigSubsection()
config.tv.lastservice = ConfigText()
config.tv.lastroot = ConfigText()
config.radio = ConfigSubsection()
config.radio.lastservice = ConfigText()
config.radio.lastroot = ConfigText()
config.servicelist = ConfigSubsection()
config.servicelist.lastmode = ConfigText(default="tv")
config.servicelist.startupservice = ConfigText()
config.servicelist.startupservice_standby = ConfigText()
config.servicelist.startupservice_onstandby = ConfigYesNo(default=False)
config.servicelist.startuproot = ConfigText()
config.servicelist.startupmode = ConfigText(default="tv")


class ChannelSelection(ChannelSelectionBase, ChannelSelectionEdit, ChannelSelectionEPG, SelectionEventInfo):
	instance = None

	def __init__(self, session):
		ChannelSelectionBase.__init__(self, session)
		if config.usage.use_pig.value:
			self.skinName = ["ChannelSelection_PIG", "ChannelSelection"]
		elif config.usage.servicelist_mode.value == "simple":
			self.skinName = ["SlimChannelSelection", "SimpleChannelSelection", "ChannelSelection"]
		else:
			self.skinName = ["ChannelSelection"]
		self["actions"] = ActionMap(["OkCancelActions", "TvRadioActions"], {
			"cancel": self.cancel,
			"ok": self.channelSelected,
			"keyRadio": self.toogleTvRadio,
			"keyTV": self.toogleTvRadio,
		})
		ChannelSelectionEPG.__init__(self)
		ChannelSelectionEdit.__init__(self)
		SelectionEventInfo.__init__(self)
		self.radioTV = 0
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
			iPlayableService.evStart: self.__evServiceStart,
			iPlayableService.evEnd: self.__evServiceEnd
		})
		try:
			if ChannelSelection.instance:
				raise AssertionError("[ChannelSelection] Class InfoBar is a singleton class and just one instance of this class is allowed!")
		except Exception:
			pass
		ChannelSelection.instance = self
		self.startServiceRef = None
		self.history_tv = []
		self.history_radio = []
		self.history = self.history_tv
		self.history_pos = 0
		self.delhistpoint = None
		if config.servicelist.startupservice.value and config.servicelist.startuproot.value:
			config.servicelist.lastmode.value = config.servicelist.startupmode.value
			if config.servicelist.lastmode.value == "tv":
				config.tv.lastservice.value = config.servicelist.startupservice.value
				config.tv.lastroot.value = config.servicelist.startuproot.value
			elif config.servicelist.lastmode.value == "radio":
				config.radio.lastservice.value = config.servicelist.startupservice.value
				config.radio.lastroot.value = config.servicelist.startuproot.value
		self.lastservice = config.tv.lastservice
		self.lastroot = config.tv.lastroot
		self.revertMode = None
		config.usage.multibouquet.addNotifier(self.multibouquet_config_changed)
		self.new_service_played = False
		self.dopipzap = False
		if config.misc.remotecontrol_text_support.value:
			self.onExecBegin.append(self.asciiOff)
		else:
			self.onExecBegin.append(self.asciiOn)
		self.mainScreenMode = None
		self.mainScreenRoot = None
		self.lastChannelRootTimer = eTimer()
		self.lastChannelRootTimer.callback.append(self.__onCreate)
		self.lastChannelRootTimer.start(100, True)
		self.pipzaptimer = eTimer()

	def asciiOn(self):
		rcinput = eRCInput.getInstance()
		rcinput.setKeyboardMode(rcinput.kmAscii)

	def asciiOff(self):
		rcinput = eRCInput.getInstance()
		rcinput.setKeyboardMode(rcinput.kmNone)

	def multibouquet_config_changed(self, val):
		self.getBouquetMode()

	def __evServiceStart(self):
		if self.dopipzap and hasattr(self.session, "pip"):
			self.servicelist.setPlayableIgnoreService(self.session.pip.getCurrentServiceReference() or eServiceReference())
		else:
			service = self.session.nav.getCurrentService()
			if service:
				info = service.info()
				if info:
					refstr = info.getInfoString(iServiceInformation.sServiceref)
					self.servicelist.setPlayableIgnoreService(eServiceReference(refstr))

	def __evServiceEnd(self):
		self.servicelist.setPlayableIgnoreService(eServiceReference())

	def setMode(self):
		self.rootChanged = True
		self.restoreRoot()
		lastservice = eServiceReference(self.lastservice.value)
		if lastservice.valid():
			self.setCurrentSelection(lastservice)

	def toogleTvRadio(self):
		if self.radioTV == 1:
			self.radioTV = 0
			self.setModeTv()
		else:
			self.radioTV = 1
			self.setModeRadio()

	def setModeTv(self):
		if self.revertMode is None and config.servicelist.lastmode.value == "radio":
			self.revertMode = MODE_RADIO
		self.history = self.history_tv
		self.lastservice = config.tv.lastservice
		self.lastroot = config.tv.lastroot
		config.servicelist.lastmode.value = "tv"
		self.setTvMode()
		self.setMode()

	def setModeRadio(self):
		if self.revertMode is None and config.servicelist.lastmode.value == "tv":
			self.revertMode = MODE_TV
		if config.usage.e1like_radio_mode.value:
			self.history = self.history_radio
			self.lastservice = config.radio.lastservice
			self.lastroot = config.radio.lastroot
			config.servicelist.lastmode.value = "radio"
			self.setRadioMode()
			self.setMode()

	def __onCreate(self):
		if config.usage.e1like_radio_mode.value:
			if config.servicelist.lastmode.value == "tv":
				self.setModeTv()
			else:
				self.setModeRadio()
		else:
			self.setModeTv()
		lastservice = eServiceReference(self.lastservice.value)
		if lastservice.valid():
			self.zap()

	def channelSelected(self):
		ref = self.getCurrentSelection()
		try:
			doClose = not config.usage.servicelistpreview_mode.value or ref == self.session.nav.getCurrentlyPlayingServiceOrGroup()
		except Exception:
			doClose = False
		if self.startServiceRef is None and not doClose:
			self.startServiceRef = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		ref = self.getCurrentSelection()
		if self.movemode and (self.isBasePathEqual(self.bouquet_root) or "userbouquet." in ref.toString()):
			self.toggleMoveMarked()
		elif (ref.flags & eServiceReference.flagDirectory) == eServiceReference.flagDirectory:
			if parentalControl.isServicePlayable(ref, self.bouquetParentalControlCallback, self.session):
				self.enterPath(ref)
				self.gotoCurrentServiceOrProvider(ref)
		elif self.bouquet_mark_edit != EDIT_OFF:
			if not (self.bouquet_mark_edit == EDIT_ALTERNATIVES and ref.flags & eServiceReference.isGroup):
				self.doMark()
		elif not (ref.flags & eServiceReference.isMarker or ref.type == -1):
			root = self.getRoot()
			if not root or not (root.flags & eServiceReference.isGroup):
				self.zap(enable_pipzap=doClose, preview_zap=not doClose)
				self.asciiOff()
				if doClose:
					if self.dopipzap:
						self.zapBack()
					self.startServiceRef = None
					self.startRoot = None
					self.correctChannelNumber()
					self.movemode and self.toggleMoveMode()
					self.editMode = False
					self.protectContextMenu = True
					self["key_green"].setText(_("Reception Lists"))
					self.close(ref)

	def bouquetParentalControlCallback(self, ref):
		self.enterPath(ref)
		self.gotoCurrentServiceOrProvider(ref)

	def togglePipzap(self):
		assert self.session.pip
		title = self.instance.getTitle()
		pos = title.find(" (")
		if pos != -1:
			title = title[:pos]
		if self.dopipzap:
			# Mark PiP as inactive and effectively deactivate pipzap.
			self.hidePipzapMessage()
			self.dopipzap = False
			# Disable PiP if not playing a service.
			if self.session.pip.pipservice is None:
				self.session.pipshown = False
				del self.session.pip
			self.__evServiceStart()
			# Move to playing service.
			lastservice = eServiceReference(self.lastservice.value)
			if lastservice.valid() and self.getCurrentSelection() != lastservice:
				self.setCurrentSelection(lastservice)
			title = "%s %s" % (title, _("(TV)"))
		else:
			# Mark PiP as active and effectively active pipzap.
			self.showPipzapMessage()
			self.dopipzap = True
			self.__evServiceStart()
			# Move to service playing in pip (will not work with sub-services).
			self.setCurrentSelection(self.session.pip.getCurrentService())
			title = "%s %s" % (title, _("(PiP)"))
		self.setTitle(title)
		print("[ChannelSelection] togglePipzap DEBUG: Setting title='%s'." % self.getTitle())
		self.buildTitle()

	def showPipzapMessage(self):
		time = config.usage.infobar_timeout.index
		if time:
			self.pipzaptimer.callback.append(self.hidePipzapMessage)
			self.pipzaptimer.startLongTimer(time)
		self.session.pip.active()

	def hidePipzapMessage(self):
		if self.pipzaptimer.isActive():
			self.pipzaptimer.callback.remove(self.hidePipzapMessage)
			self.pipzaptimer.stop()
		if hasattr(self.session, "pip"):
			self.session.pip.inactive()

	def zap(self, enable_pipzap=False, preview_zap=False, checkParentalControl=True, ref=None):
		self.curRoot = self.startRoot
		nref = ref or self.getCurrentSelection()
		wrappererror = None
		for p in plugins.getPlugins(PluginDescriptor.WHERE_CHANNEL_ZAP):
			(newurl, errormsg) = p(session=self.session, service=nref)
			if errormsg:
				wrappererror = _("Error getting link via %s\n%s") % (p.name, errormsg)
				break
			elif newurl:
				nref.setAlternativeUrl(newurl)
				break
		if wrappererror:
			AddPopup(text=wrappererror, type=MessageBox.TYPE_ERROR, timeout=5, id="channelzapwrapper")
		ref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if enable_pipzap and self.dopipzap:
			ref = self.session.pip.getCurrentService()
			if ref is None or ref != nref:
				nref = self.session.pip.resolveAlternatePipService(nref)
				if nref and (not checkParentalControl or parentalControl.isServicePlayable(nref, boundFunction(self.zap, enable_pipzap=True, checkParentalControl=False))):
					self.session.pip.playService(nref)
					self.__evServiceStart()
					self.showPipzapMessage()
				else:
					self.setStartRoot(self.curRoot)
					self.setCurrentSelection(ref)
		elif ref is None or ref != nref:
			Screens.InfoBar.InfoBar.instance.checkTimeshiftRunning(boundFunction(self.zapCheckTimeshiftCallback, enable_pipzap, preview_zap, nref))
		elif not preview_zap:
			self.saveRoot()
			self.saveChannel(nref)
			config.servicelist.lastmode.save()
			self.setCurrentSelection(nref)
			if self.startServiceRef is None or nref != self.startServiceRef:
				self.addToHistory(nref)
			self.rootChanged = False
			self.revertMode = None

	def zapCheckTimeshiftCallback(self, enable_pipzap, preview_zap, nref, answer):
		if answer:
			self.new_service_played = True
			self.session.nav.playService(nref)
			if not preview_zap:
				self.saveRoot()
				self.saveChannel(nref)
				config.servicelist.lastmode.save()
				if self.startServiceRef is None or nref != self.startServiceRef:
					self.addToHistory(nref)
				if self.dopipzap:
					self.setCurrentSelection(self.session.pip.getCurrentService())
				else:
					self.mainScreenMode = config.servicelist.lastmode.value
					self.mainScreenRoot = self.getRoot()
				self.revertMode = None
			else:
				RemovePopup("Parental control")
				self.setCurrentSelection(nref)
		else:
			self.setStartRoot(self.curRoot)
			self.setCurrentSelection(self.session.nav.getCurrentlyPlayingServiceOrGroup())
		if not preview_zap:
			self.hide()

	def newServicePlayed(self):
		ret = self.new_service_played
		self.new_service_played = False
		return ret

	def addToHistory(self, ref):
		if self.delhistpoint is not None:
			x = self.delhistpoint
			while x <= len(self.history) - 1:
				del self.history[x]
		self.delhistpoint = None
		if self.servicePath is not None:
			tmp = self.servicePath[:]
			tmp.append(ref)
			self.history.append(tmp)
			hlen = len(self.history)
			x = 0
			while x < hlen - 1:
				if self.history[x][-1] == ref:
					del self.history[x]
					hlen -= 1
				else:
					x += 1
			if hlen > HISTORY_SIZE:
				del self.history[0]
				hlen -= 1
			self.history_pos = hlen - 1

	def historyBack(self):
		hlen = len(self.history)
		currentPlayedRef = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if hlen > 0 and currentPlayedRef and self.history[self.history_pos][-1] != currentPlayedRef:
			self.addToHistory(currentPlayedRef)
			hlen = len(self.history)
		if hlen > 1 and self.history_pos > 0:
			self.history_pos -= 1
			self.setHistoryPath()
		self.delhistpoint = self.history_pos + 1

	def historyNext(self):
		self.delhistpoint = None
		hlen = len(self.history)
		if hlen > 1 and self.history_pos < hlen - 1:
			self.history_pos += 1
			self.setHistoryPath()

	def setHistoryPath(self, doZap=True):
		path = self.history[self.history_pos][:]
		ref = path.pop()
		del self.servicePath[:]
		self.servicePath += path
		self.saveRoot()
		root = path[-1]
		cur_root = self.getRoot()
		if cur_root and cur_root != root:
			self.setRoot(root)
		self.servicelist.setCurrent(ref)
		if doZap:
			self.session.nav.playService(ref)
		if self.dopipzap:
			self.setCurrentSelection(self.session.pip.getCurrentService())
		else:
			self.setCurrentSelection(ref)
		self.saveChannel(ref)

	def historyClear(self):
		if self and self.servicelist:
			for i in list(range(0, len(self.history) - 1)):
				del self.history[0]
			self.history_pos = len(self.history) - 1
			return True
		return False

	def historyZap(self, direction):
		count = len(self.history)
		if count > 0:
			# markedItem = self.history_pos
			selectedItem = self.history_pos + direction
			if selectedItem < 0:
				selectedItem = 0
			elif selectedItem > count - 1:
				selectedItem = count - 1
			# serviceHandler = eServiceCenter.getInstance()
			# historyInfoList = []
			# for item in self.history:
			# 	# info = serviceHandler.info(item[-1])
			# 	# if info:
			# 	# 	historyInfoList.append((info.getName(item[-1]), item[-1]))
			# 	historyList.append(item[-1])
			# self.session.openWithCallback(self.historyMenuClosed, HistoryZapSelector, historyList, selectedItem, mark, invert_items=True, redirect_buttons=True, wrap_around=True)
			self.session.openWithCallback(self.historyMenuClosed, HistoryZapSelector, [x[-1] for x in self.history], markedItem=self.history_pos, selectedItem=selectedItem)

	def historyMenuClosed(self, retval):
		if not retval:
			return
		hlen = len(self.history)
		pos = 0
		for x in self.history:
			if x[-1] == retval:
				break
			pos += 1
		self.delhistpoint = pos + 1
		if pos < hlen and pos != self.history_pos:
			tmp = self.history[pos]
			# self.history.append(tmp)
			# del self.history[pos]
			self.history_pos = pos
			self.setHistoryPath()

	def saveRoot(self):
		path = ""
		for i in self.servicePath:
			path += i.toString()
			path += ";"
		if path and path != self.lastroot.value:
			if self.mode == MODE_RADIO and "FROM BOUQUET \"bouquets.tv\"" in path:
				self.setModeTv()
			elif self.mode == MODE_TV and "FROM BOUQUET \"bouquets.radio\"" in path:
				self.setModeRadio()
			self.lastroot.value = path
			self.lastroot.save()

	def restoreRoot(self):
		tmp = [x for x in self.lastroot.value.split(";") if x != ""]
		current = [x.toString() for x in self.servicePath]
		if tmp != current or self.rootChanged:
			self.clearPath()
			cnt = 0
			for i in tmp:
				self.servicePath.append(eServiceReference(i))
				cnt += 1
			if cnt:
				path = self.servicePath.pop()
				self.enterPath(path)
			else:
				self.showFavourites()
				self.saveRoot()
			self.rootChanged = False

	def preEnterPath(self, refstr):
		if self.servicePath and self.servicePath[0] != eServiceReference(refstr):
			pathstr = self.lastroot.value
			if pathstr is not None and refstr in pathstr:
				self.restoreRoot()
				lastservice = eServiceReference(self.lastservice.value)
				if lastservice.valid():
					self.setCurrentSelection(lastservice)
				return True
		return False

	def saveChannel(self, ref):
		if ref is not None:
			refstr = ref.toString()
		else:
			refstr = ""
		if refstr != self.lastservice.value:
			self.lastservice.value = refstr
			self.lastservice.save()

	def setCurrentServicePath(self, path, doZap=True):
		if self.history:
			self.history[self.history_pos] = path
		else:
			self.history.append(path)
		self.setHistoryPath(doZap)

	def getCurrentServicePath(self):
		if self.history:
			return self.history[self.history_pos]
		return None

	def recallPrevService(self):
		hlen = len(self.history)
		currentPlayedRef = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if hlen > 0 and currentPlayedRef and self.history[self.history_pos][-1] != currentPlayedRef:
			self.addToHistory(currentPlayedRef)
			hlen = len(self.history)
		if hlen > 1:
			if self.history_pos == hlen - 1:
				tmp = self.history[self.history_pos]
				self.history[self.history_pos] = self.history[self.history_pos - 1]
				self.history[self.history_pos - 1] = tmp
			else:
				tmp = self.history[self.history_pos + 1]
				self.history[self.history_pos + 1] = self.history[self.history_pos]
				self.history[self.history_pos] = tmp
			self.setHistoryPath()

	def cancel(self):
		if self.movemode:
			self.toggleMoveMode()
		if self.revertMode is None:
			self.restoreRoot()
			if self.dopipzap:
				# This unfortunately won't work with sub-services.
				self.setCurrentSelection(self.session.pip.getCurrentService())
			else:
				lastservice = eServiceReference(self.lastservice.value)
				if lastservice.valid() and self.getCurrentSelection() != lastservice:
					self.setCurrentSelection(lastservice)
		elif self.revertMode == MODE_TV and self.mode == MODE_RADIO:
			self.setModeTv()
		elif self.revertMode == MODE_RADIO and self.mode == MODE_TV:
			self.setModeRadio()
		self.asciiOff()
		if config.usage.servicelistpreview_mode.value:
			self.zapBack()
		self.correctChannelNumber()
		self.editMode = False
		self.protectContextMenu = True
		self.close(None)

	def zapBack(self):
		currentPlayedRef = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if self.startServiceRef and (currentPlayedRef is None or currentPlayedRef != self.startServiceRef):
			self.setStartRoot(self.startRoot)
			self.new_service_played = True
			self.session.nav.playService(self.startServiceRef)
			self.saveChannel(self.startServiceRef)
		else:
			self.restoreMode()
		self.startServiceRef = None
		self.startRoot = None
		if self.dopipzap:
			# This unfortunately won't work with sub-services.
			self.setCurrentSelection(self.session.pip.getCurrentService())
		else:
			lastservice = eServiceReference(self.lastservice.value)
			if lastservice.valid() and self.getCurrentSelection() == lastservice:
				pass  # Keep current selection.
			else:
				self.setCurrentSelection(currentPlayedRef)

	def setStartRoot(self, root):
		if root:
			if self.revertMode == MODE_TV:
				self.setModeTv()
			elif self.revertMode == MODE_RADIO:
				self.setModeRadio()
			self.revertMode = None
			self.enterUserbouquet(root)

	def restoreMode(self):
		if self.revertMode == MODE_TV:
			self.setModeTv()
		elif self.revertMode == MODE_RADIO:
			self.setModeRadio()
		self.revertMode = None

	def correctChannelNumber(self):
		current_ref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		if self.dopipzap:
			tmp_mode = config.servicelist.lastmode.value
			tmp_root = self.getRoot()
			tmp_ref = self.getCurrentSelection()
			pip_ref = self.session.pip.getCurrentService()
			if tmp_ref and pip_ref and tmp_ref != pip_ref:
				self.revertMode = None
				return
			if self.mainScreenMode == "tv":
				self.setModeTv()
			elif self.mainScreenMode == "radio":
				self.setModeRadio()
			if self.mainScreenRoot:
				self.setRoot(self.mainScreenRoot)
				self.setCurrentSelection(current_ref)
		selected_ref = self.getCurrentSelection()
		if selected_ref and current_ref and selected_ref.getChannelNum() != current_ref.getChannelNum():
			oldref = self.session.nav.currentlyPlayingServiceReference
			if oldref and selected_ref == oldref or (oldref != current_ref and selected_ref == current_ref):
				self.session.nav.currentlyPlayingServiceOrGroup = selected_ref
				self.session.nav.pnav.navEvent(iPlayableService.evStart)
		if self.dopipzap:
			if tmp_mode == "tv":
				self.setModeTv()
			elif tmp_mode == "radio":
				self.setModeRadio()
			self.enterUserbouquet(tmp_root)
			title = self.instance.getTitle()
			pos = title.find(" (")
			if pos != -1:
				# title = title[:pos]
				# title += _(" (PiP)")
				self.setTitle("%s %s" % (title[:pos], _("(PiP)")))
				print("[ChannelSelection] correctChannelNumber DEBUG: Setting title='%s'." % self.getTitle())
				self.buildTitle()
			if tmp_ref and pip_ref and tmp_ref.getChannelNum() != pip_ref.getChannelNum():
				self.session.pip.currentService = tmp_ref
			self.setCurrentSelection(tmp_ref)
		self.revertMode = None

	def switchToAll(self, sref):
		if Screens.InfoBar.InfoBar.instance:
			servicelist = Screens.InfoBar.InfoBar.instance.servicelist
			if servicelist:
				refStr = sref.toString()
				sType = refStr.split(":", maxsplit=3)
				if len(sType) == 4 and sType[2] in ("2", "A") and config.usage.e1like_radio_mode.value:
					typestr = "radio"
					if servicelist.mode != 1:
						servicelist.setModeRadio()
						servicelist.radioTV = 1
					bouquet = eServiceReference("%s ORDER BY name" % service_types_radio)
				else:
					typestr = "tv"
					if servicelist.mode != 0:
						servicelist.setModeTv()
						servicelist.radioTV = 0
					bouquet = eServiceReference("%s ORDER BY name" % service_types_tv)
				servicelist.clearPath()
				if config.usage.multibouquet.value:
					rootBouquet = eServiceReference("1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"bouquets.%s\" ORDER BY bouquet" % typestr)
					if servicelist.bouquet_root != rootBouquet:
						servicelist.bouquet_root = rootBouquet
				servicelist.enterPath(bouquet)
				servicelist.setCurrentSelection(sref)
				servicelist.zap(enable_pipzap=True)
				servicelist.correctChannelNumber()
				servicelist.startRoot = bouquet
				if servicelist.dopipzap:
					servicelist.addToHistory(sref)

	def performZap(self, sref):
		def getBqRoot(reference):
			reference = reference.toString()
			isTV = True
			sType = reference.split(":", maxsplit=3)
			if len(sType) == 4 and sType[2] in ("2", "A") and config.usage.e1like_radio_mode.value:
				isTV = False
				if config.usage.multibouquet.value:
					bqRootStr = "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"bouquets.radio\" ORDER BY bouquet"
				else:
					return (singlebouquet_radio_ref, False)
			else:
				if config.usage.multibouquet.value:
					bqRootStr = "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"bouquets.tv\" ORDER BY bouquet"
				else:
					return (singlebouquet_tv_ref, True)
			return (eServiceReference(bqRootStr), isTV)

		def finalZap(isTV, servicepath):
			if "current" in servicepath:
				self.saveChannel(sref)
				self.setCurrentSelection(sref)
				self.zap(enable_pipzap=True)
				if self.dopipzap:
					self.addToHistory(sref)
				return
			if isTV and self.mode != 0:
				self.setModeTv()
				self.radioTV = 0
			if not isTV and self.mode != 1:
				self.setModeRadio()
				self.radioTV = 1
			self.clearPath()
			for bouquet in servicepath.split(";"):
				if bouquet:
					self.enterPath(eServiceReference(bouquet))
			self.setCurrentSelection(sref)
			self.zap(enable_pipzap=True)
			self.correctChannelNumber()
			self.startRoot = bouquet
			if self.dopipzap:
				self.addToHistory(sref)

		def walk(serviceHandler, bouquet, level=0):
			servicelist = serviceHandler.list(bouquet)
			if servicelist is not None:
				service = servicelist.getNext()
				while service.valid():
					if service.flags & eServiceReference.isDirectory:
						if level == 0 and "userbouquet.LastScanned.tv" in service.toString():  # Don't search in LastScanned.
							service = servicelist.getNext()
							continue
						found = walk(serviceHandler, service, level + 1)
						if found:
							return "%s;%s" % (bouquet.toString(), found)
					elif service == sref:
						if bouquet != self.getRoot():
							if config.usage.multibouquet.value:
								return "%s;" % bouquet.toString()
							else:
								return bouquet.toString()
						else:
							return "current"  # Fast zap if channel found in current bouquet.
					service = servicelist.getNext()
			return None

		serviceHandler = eServiceCenter.getInstance()
		bouquet, isTV = getBqRoot(sref)
		found = walk(serviceHandler, bouquet)
		if found:
			finalZap(isTV, found)
		else:
			self.switchToAll(sref)


class PiPZapSelection(ChannelSelection):
	def __init__(self, session):
		ChannelSelection.__init__(self, session)
		self.skinName = ["SlimChannelSelection", "SimpleChannelSelection", "ChannelSelection"]
		self.startservice = None
		self.pipzapfailed = None
		if plugin_PiPServiceRelation_installed:
			self.pipServiceRelation = getRelationDict()
		else:
			self.pipServiceRelation = {}
		self.keymaptimer = eTimer()
		self.keymaptimer.callback.append(self.enableKeyMap)
		self.onShown.append(self.disableKeyMap)

	def disableKeyMap(self):
		if not hasattr(self.session, "pip"):
			if not self.pipzapfailed:
				self.startservice = self.session.nav.getCurrentlyPlayingServiceReference() or self.servicelist.getCurrent()
			else:
				self.startservice = self.startservice
			self.setCurrentSelection(self.startservice)
			self.session.pip = self.session.instantiateDialog(PictureInPicture)
			self.session.pip.show()
			self.session.pip.playService(self.startservice)
		eActionMap.getInstance().unbindNativeKey("ListboxActions", 0)
		eActionMap.getInstance().unbindNativeKey("ListboxActions", 1)
		self.keymaptimer.start(1000, True)

	def enableKeyMap(self):
		eActionMap.getInstance().bindKey("keymap.xml", "generic", 103, 5, "ListboxActions", "moveUp")
		eActionMap.getInstance().bindKey("keymap.xml", "generic", 108, 5, "ListboxActions", "moveDown")

	def channelSelected(self):
		ref = self.servicelist.getCurrent()
		if (ref.flags & eServiceReference.flagDirectory) == eServiceReference.flagDirectory:
			self.enterPath(ref)
			self.gotoCurrentServiceOrProvider(ref)
		elif not (ref.flags & eServiceReference.isMarker or ref.toString().startswith("-1")):
			root = self.getRoot()
			if not root or not (root.flags & eServiceReference.isGroup):
				n_service = self.pipServiceRelation.get(str(ref), None)
				if n_service is not None:
					newservice = eServiceReference(n_service)
				else:
					newservice = ref
				if not hasattr(self.session, "pip"):
					self.session.pip = self.session.instantiateDialog(PictureInPicture)
					self.session.pip.show()
				if self.session.pip.playService(newservice):
					self.pipzapfailed = False
					self.session.pipshown = True
					self.session.pip.servicePath = self.getCurrentServicePath()
					self.setStartRoot(self.curRoot)
					self.saveRoot()
					self.saveChannel(ref)
					self.setCurrentSelection(ref)
					if BoxInfo.getItem("LCDMiniTVPiP") and int(config.lcd.modepip.value) >= 1:
						print("[ChannelSelection] LCDMiniTV enable PiP.")
						f = open("/proc/stb/lcd/mode", "w")
						f.write(config.lcd.modepip.value)
						f.close()
						f = open("/proc/stb/vmpeg/1/dst_width", "w")
						f.write("0")
						f.close()
						f = open("/proc/stb/vmpeg/1/dst_height", "w")
						f.write("0")
						f.close()
						f = open("/proc/stb/vmpeg/1/dst_apply", "w")
						f.write("1")
						f.close()
					self.close(True)
				else:
					self.pipzapfailed = True
					self.session.pipshown = False
					del self.session.pip
					if BoxInfo.getItem("LCDMiniTVPiP") and int(config.lcd.modepip.value) >= 1:
							print("[ChannelSelection] LCDMiniTV disable PiP.")
							fileWriteLine("/proc/stb/lcd/mode", str(config.lcd.modeminitv.value))
					self.close(None)

	def cancel(self):
		self.asciiOff()
		if self.startservice and hasattr(self.session, "pip") and self.session.pip.getCurrentService() and self.startservice == self.session.pip.getCurrentService():
			self.session.pipshown = False
			del self.session.pip
			if BoxInfo.getItem("LCDMiniTVPiP") and int(config.lcd.modepip.value) >= 1:
				print("[ChannelSelection] LCDMiniTV disable PiP.")
				fileWriteLine("/proc/stb/lcd/mode", str(config.lcd.modeminitv.value))
		self.correctChannelNumber()
		self.close(None)


class RadioInfoBar(Screen):
	def __init__(self, session):
		Screen.__init__(self, session)
		self.setTitle(_("Radio Channel Selection"))
		print("[ChannelSelection] RadioInfoBar DEBUG: Setting title='%s'." % self.getTitle())
		self["RdsDecoder"] = RdsDecoder(self.session.nav)


class ChannelSelectionRadio(ChannelSelectionBase, ChannelSelectionEdit, ChannelSelectionEPG, InfoBarBase, SelectionEventInfo):
	ALLOW_SUSPEND = True

	def __init__(self, session, infobar):
		ChannelSelectionBase.__init__(self, session)
		InfoBarBase.__init__(self)
		SelectionEventInfo.__init__(self)
		self.infobar = infobar
		self.startServiceRef = None
		self.onLayoutFinish.append(self.onCreate)
		self.info = session.instantiateDialog(RadioInfoBar)  # Our simple InfoBar.
		self.info.setAnimationMode(0)
		self["actions"] = ActionMap(["OkCancelActions", "TvRadioActions"], {
			"keyTV": self.cancel,
			"keyRadio": self.cancel,
			"cancel": self.cancel,
			"ok": self.channelSelected,
		})
		ChannelSelectionEPG.__init__(self)
		ChannelSelectionEdit.__init__(self)
		self.__event_tracker = ServiceEventTracker(screen=self, eventmap={
			iPlayableService.evStart: self.__evServiceStart,
			iPlayableService.evEnd: self.__evServiceEnd
		})
		# RDS Radiotext / Rass Support begin.
		self.infobar = infobar  # Reference to real InfoBar (the one and only).
		self["RdsDecoder"] = self.info["RdsDecoder"]
		self["RdsActions"] = HelpableActionMap(self, ["InfobarRdsActions"], {
			"startRassInteractive": (self.startRassInteractive, _("View Rass interactive..."))
		}, prio=-1)
		self["RdsActions"].setEnabled(False)
		infobar.rds_display.onRassInteractivePossibilityChanged.append(self.RassInteractivePossibilityChanged)
		self.onClose.append(self.__onClose)
		self.onExecBegin.append(self.__onExecBegin)
		self.onExecEnd.append(self.__onExecEnd)

	def __onClose(self):
		del self.info["RdsDecoder"]
		self.session.deleteDialog(self.info)
		self.infobar.rds_display.onRassInteractivePossibilityChanged.remove(self.RassInteractivePossibilityChanged)
		lastservice = eServiceReference(config.tv.lastservice.value)
		self.session.nav.playService(lastservice)

	def startRassInteractive(self):
		self.info.hide()
		self.infobar.rass_interactive = self.session.openWithCallback(self.RassInteractiveClosed, RassInteractive)

	def RassInteractiveClosed(self):
		self.info.show()
		self.infobar.rass_interactive = None
		self.infobar.RassSlidePicChanged()

	def RassInteractivePossibilityChanged(self, state):
		self["RdsActions"].setEnabled(state)

	def __onExecBegin(self):
		self.info.show()

	def __onExecEnd(self):
		self.info.hide()

	def cancel(self):
		self.info.hide()
		self.close(None)

	def __evServiceStart(self):
		service = self.session.nav.getCurrentService()
		if service:
			info = service.info()
			if info:
				refstr = info.getInfoString(iServiceInformation.sServiceref)
				self.servicelist.setPlayableIgnoreService(eServiceReference(refstr))

	def __evServiceEnd(self):
		self.servicelist.setPlayableIgnoreService(eServiceReference())

	def saveRoot(self):
		path = ""
		for i in self.servicePathRadio:
			path += i.toString()
			path += ";"
		if path and path != config.radio.lastroot.value:
			config.radio.lastroot.value = path
			config.radio.lastroot.save()

	def restoreRoot(self):
		tmp = [x for x in config.radio.lastroot.value.split(";") if x != ""]
		current = [x.toString() for x in self.servicePath]
		if tmp != current or self.rootChanged:
			cnt = 0
			for i in tmp:
				self.servicePathRadio.append(eServiceReference(i))
				cnt += 1
			if cnt:
				path = self.servicePathRadio.pop()
				self.enterPath(path)
			else:
				self.showFavourites()
				self.saveRoot()
			self.rootChanged = False

	def preEnterPath(self, refstr):
		if self.servicePathRadio and self.servicePathRadio[0] != eServiceReference(refstr):
			pathstr = config.radio.lastroot.value
			if pathstr is not None and refstr in pathstr:
				self.restoreRoot()
				lastservice = eServiceReference(config.radio.lastservice.value)
				if lastservice.valid():
					self.setCurrentSelection(lastservice)
				return True
		return False

	def onCreate(self):
		self.setRadioMode()
		self.restoreRoot()
		lastservice = eServiceReference(config.radio.lastservice.value)
		if lastservice.valid():
			self.servicelist.setCurrent(lastservice)
			self.session.nav.playService(lastservice)
		else:
			self.session.nav.stopService()
		self.info.show()

	def channelSelected(self, doClose=False):  # Just return selected service.
		ref = self.getCurrentSelection()
		if self.movemode:
			self.toggleMoveMarked()
		elif (ref.flags & eServiceReference.flagDirectory) == eServiceReference.flagDirectory:
			self.enterPath(ref)
			self.gotoCurrentServiceOrProvider(ref)
		elif self.bouquet_mark_edit != EDIT_OFF:
			if not (self.bouquet_mark_edit == EDIT_ALTERNATIVES and ref.flags & eServiceReference.isGroup):
				self.doMark()
		elif not (ref.flags & eServiceReference.isMarker):  # No marker.
			cur_root = self.getRoot()
			if not cur_root or not (cur_root.flags & eServiceReference.isGroup):
				playingref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
				if playingref is None or playingref != ref:
					self.session.nav.playService(ref)
					config.radio.lastservice.value = ref.toString()
					config.radio.lastservice.save()
				self.saveRoot()

	def zapBack(self):
		self.channelSelected()


class SimpleChannelSelection(ChannelSelectionBase):
	def __init__(self, session, title, currentBouquet=False):
		ChannelSelectionBase.__init__(self, session)
		self["actions"] = ActionMap(["OkCancelActions", "TvRadioActions"], {
			"cancel": self.close,
			"ok": self.channelSelected,
			"keyRadio": self.setModeRadio,
			"keyTV": self.setModeTv,
		})
		self.bouquet_mark_edit = EDIT_OFF
		self.title = title
		self.currentBouquet = currentBouquet
		self.onLayoutFinish.append(self.layoutFinished)

	def layoutFinished(self):
		self.setModeTv()
		if self.currentBouquet:
			ref = Screens.InfoBar.InfoBar.instance.servicelist.getRoot()
			if ref:
				self.enterPath(ref)
				self.gotoCurrentServiceOrProvider(ref)

	def saveRoot(self):
		pass

	def keyRecord(self):
		return 0

	def channelSelected(self):  # Just return selected service.
		ref = self.getCurrentSelection()
		if (ref.flags & eServiceReference.flagDirectory) == eServiceReference.flagDirectory:
			self.enterPath(ref)
			self.gotoCurrentServiceOrProvider(ref)
		elif not (ref.flags & eServiceReference.isMarker):
			ref = self.getCurrentSelection()
			self.close(ref)

	def setModeTv(self):
		self.setTvMode()
		self.showFavourites()

	def setModeRadio(self):
		self.setRadioMode()
		self.showFavourites()


class BouquetSelector(Screen, HelpableScreen):
	def __init__(self, session, bouquets, selectedFunc, enableWrapAround=None):
		Screen.__init__(self, session)
		HelpableScreen.__init__(self)
		self.setTitle(_("Bouquet Selector"))
		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("Select"))
		self["menu"] = MenuList([(x[0], x[1]) for x in bouquets])
		self.selectedFunc = selectedFunc
		self["actions"] = HelpableActionMap(self, ["SelectCancelActions"], {
			"cancel": (self.keyCancel, _("Cancel the bouquet selection")),
			"select": (self.keySelect, _("Select the currently highlighted bouquet"))
		}, prio=0, description=_("Bouquet Selector Actions"))
		self["navigationActions"] = HelpableActionMap(self, ["NavigationActions"], {
			"top": (self.keyTop, _("Move to the first line / screen")),
			"pageUp": (self.keyPageUp, _("Move up a screen")),
			"up": (self.keyUp, _("Move up a line")),
			"down": (self.keyDown, _("Move down a line")),
			"pageDown": (self.keyPageDown, _("Move down a screen")),
			"bottom": (self.keyBottom, _("Move to the last line / screen"))
		}, prio=0, description=_("Bouquet Selector Navigation Actions"))
		self.onLayoutFinish.append(self.layoutFinished)

	def layoutFinished(self):
		self["menu"].enableAutoNavigation(False)

	def keyCancel(self):
		self.close(False)

	def keySelect(self):
		current = self["menu"].getCurrent()
		self.selectedFunc(current and current[1])

	def keyTop(self):
		self["menu"].goTop()

	def keyPageUp(self):
		self["menu"].goPageUp()

	def keyUp(self):
		self["menu"].goLineUp()

	def keyDown(self):
		self["menu"].goLineDown()

	def keyPageDown(self):
		self["menu"].goPageDown()

	def keyBottom(self):
		self["menu"].goBottom()


class EPGBouquetSelector(BouquetSelector):
	def __init__(self, session, bouquets, selectedFunc, enableWrapAround=None):
		BouquetSelector.__init__(self, session, bouquets, selectedFunc)
		self.skinName = ["EPGBouquetSelector", "BouquetSelector"]
		self.bouquets = bouquets

	def keySelect(self):
		current = self["menu"].getCurrent()
		self.selectedFunc(current and current[1], self.bouquets)


class EpgBouquetSelector(EPGBouquetSelector):
	pass


class HistoryZapSelector(Screen, HelpableScreen):
	def __init__(self, session, historyItems, markedItem=0, selectedItem=0):
		Screen.__init__(self, session)
		HelpableScreen.__init__(self)
		self.setTitle(_("History Zap"))
		print("[ChannelSelection] HistoryZapSelector DEBUG: Setting title='%s'." % self.getTitle())
		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("Select"))
		self["actions"] = HelpableActionMap(self, ["SelectCancelActions"], {
			"cancel": (self.keyCancel, _("Cancel the service zap")),
			"select": (self.keySelect, _("Select the currently highlighted service"))
		}, prio=0, description=_("History Zap Actions"))
		self["navigationActions"] = HelpableActionMap(self, ["NavigationActions"], {
			"top": (self.keyTop, _("Move to the first line / screen")),
			"pageUp": (self.keyPageUp, _("Move up a screen")),
			"up": (self.keyUp, _("Move up a line")),
			"down": (self.keyDown, _("Move down a line")),
			"pageDown": (self.keyPageDown, _("Move down a screen")),
			"bottom": (self.keyBottom, _("Move to the last line / screen"))
		}, prio=0, description=_("History Zap Navigation Actions"))
		serviceHandler = eServiceCenter.getInstance()
		historyList = []
		for index, historyItem in enumerate(historyItems):
			info = serviceHandler.info(historyItem)
			if info:
				serviceName = info.getName(historyItem) or ""
				eventName = ""
				eventDescription = ""
				eventDuration = ""
				event = info.getEvent(historyItem)
				if event:
					eventName = event.getEventName() or ""
					# eventName = eventName.replace("(18+)", "").replace("18+", "").replace("(16+)", "").replace("16+", "").replace("(12+)", "").replace("12+", "").replace("(7+)", "").replace("7+", "").replace("(6+)", "").replace("6+", "").replace("(0+)", "").replace("0+", "")  # TODO: What are all these string replacements doing?
					eventDescription = event.getShortDescription()
					if not eventDescription:
						eventDescription = event.getExtendedDescription() or ""
					begin = event.getBeginTime()
					if begin:
						end = begin + event.getDuration()
						remaining = (end - int(time())) // 60
						prefix = "+" if remaining > 0 else ""
						localBegin = localtime(begin)
						localEnd = localtime(end)
						eventDuration = _("%s  -  %s    (%s%d Min)") % (strftime(config.usage.time.short.value, localBegin), strftime(config.usage.time.short.value, localEnd), prefix, remaining)
			servicePicon = getPiconName(str(ServiceReference(historyItem)))
			servicePicon = loadPNG(servicePicon) if servicePicon else ""
			# List entries: ("", ServiceMarked, ServiceName, EventName, EventDescription, EventDuration, ServicePicon, ServiceReference)
			historyList.append(("", index == markedItem and "\u00BB" or "", serviceName, eventName, eventDescription, eventDuration, servicePicon, historyItem))
		if True:  # Newest first.
			historyList.reverse()
			self.selectedItem = len(historyList) - selectedItem - 1
		else:
			self.selectedItem = selectedItem
		self["menu"] = List(historyList)
		self.onLayoutFinish.append(self.layoutFinished)

	def layoutFinished(self):
		self["menu"].enableAutoNavigation(False)
		self["menu"].setIndex(self.selectedItem)

	def keyCancel(self):
		self.close(None)  # Send None to tell the calling code that the selection was canceled.

	def keySelect(self):
		current = self["menu"].getCurrent()
		self.close(current and current[7])  # Send the selected ServiceReference to the calling code.

	def keyTop(self):
		self["menu"].goTop()

	def keyPageUp(self):
		self["menu"].goPageUp()

	def keyUp(self):
		self["menu"].goLineUp()

	def keyDown(self):
		self["menu"].goLineDown()

	def keyPageDown(self):
		self["menu"].goPageDown()

	def keyBottom(self):
		self["menu"].goBottom()
